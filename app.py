import streamlit as st
from openai import OpenAI, RateLimitError
import backoff
import feedparser
import wikipedia
import time, datetime
import pytz
import requests
import json
import pymongo
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import urllib


OsloTZ = pytz.timezone('Europe/Oslo')

# Mongo collection names
NEWS = "newsfeed"
LOGS = "logs"
USERS = "users"
INTERESTS = "interests"


ASSISTANT_1_NAME = 'Assistant 1'
ASSISTANT_2_NAME = 'Assistant 2'


INTRO_MSG = """Hei!
Jeg er Falk, din nye nyhetsassistent fra VG.
Min oppgave er å holde deg oppdatert på nyhetene fra VG gjennom en engasjerende chat-samtale. Jeg håper å skape meningsfulle diskusjoner og gi deg innsikt i aktuelle hendelser.
Jeg oppfordrer deg til å stille spørsmål om nyhetene, dele dine meninger og tanker, eller foreslå annet innhold du er nysgjerrig på. Jeg er her for å hjelpe deg med å holde deg oppdatert og engasjert.
Nyhetene jeg deler, er basert på VG sine artikler. Hvis du har spørsmål om noe som ikke dekkes i artiklene, bruker jeg Wikipedia for å finne svar. La oss sammen utforske de siste nyhetene og ha innsiktsfulle samtaler om hva som skjer i verden!
La oss bli litt bedre kjent før vi kommer i gang med nyhetene."""

INTRO_USER_NAME = " Hva vil du at jeg skal kalle deg?"

RETURN_MSG = "Hei, %s! Ønsker du å bli oppdatert på siste nytt?"


st.set_page_config(
    page_title="NewsAgent",
    page_icon="",
    layout="wide",
    initial_sidebar_state="auto")


pincodes = st.secrets["PINCODES"]
openai_api_key = st.secrets["OPENAI_KEY"]
assistant_1_id = st.secrets["ASSISTANT1_ID"]
assistant_2_id = st.secrets["ASSISTANT2_ID"]


with st.sidebar:
    # openai_api_key = st.secrets["openai_key"] # # st.text_input("OpenAI API Key", key="chatbot_api_key", type="password")
    # "[Get an OpenAI API key](https://platform.openai.com/account/api-keys)"
    # assistant_id = st.secrets["assistant1_id"] "asst_WJWynqP6q17EbYpyzBMRGMJF" #  st.text_input("Assistant ID", key="assistant_id", type="default")
    if st.button("Reset Session"):
        # stop existing runs
        # del st.session_state["pincode"]
        st.session_state.clear()
        st.rerun()


@st.cache_resource(ttl=3600)
def get_mongo() -> MongoClient:
    uri = "mongodb+srv://{db_user}:{db_pass}@{db_host}/?retryWrites=true&w=majority&appName=AtlasCluster".format(
        db_user = st.secrets['DB_USER'], db_pass = st.secrets['DB_PASSWORD'], db_host = st.secrets['DB_HOST']
    )
    # Create a new client and connect to the server
    mongo_client = MongoClient(uri, server_api=ServerApi('1'))
    return mongo_client


@st.cache_resource(ttl=600)
def get_db():
    mongo = get_mongo()
    db = mongo[st.secrets["DB_NAME"]]
    return db


def log_msg(msg:str, is_action = False, is_user= True):
    db = get_db()
    logs_collection = db[LOGS]
    logs_collection.insert_one({
        'datetime': datetime.datetime.now(OsloTZ),
        'pincode': st.session_state.get('pincode', 0),
        # 'assistant_id': st.session_state.get('assistant_id', ''),
        # 'assistant': st.session_state.get('assistant', ''),
        'msg': msg,
        'is_user': is_user,
        'is_action': is_action
    })

def log_action(act:str):
    log_msg(act, True)

def log_reply(msg:str):
    log_msg(msg, is_user= False)


@st.cache_resource(ttl=3600)
def get_client() -> OpenAI:
    if not openai_api_key:
        st.error("Please provide Open AI API key first")
        st.stop()
    client = OpenAI(api_key=openai_api_key)
    return client


def get_thread():
    client = get_client()
    
    if "thread_id" in st.session_state:
        thread = client.beta.threads.retrieve(st.session_state["thread_id"])
    else:
        thread = client.beta.threads.create()
        st.session_state["thread_id"] = thread.id
    return thread


def get_assistant():
    # if not st.session_state["assistant_id"]:
    #     st.error("Please provide Assistant Id")
    #     st.stop()
    client = get_client()
    return client.beta.assistants.retrieve(assistant_1_id)


def call_tools(run):
    tool_outputs = []
    for tool in run.required_action.submit_tool_outputs.tool_calls:
        output = ""
        log_action("Calling tool: %s" % tool.function.name)
        try:
            match tool.function.name:
                case "get_categories":
                    st.toast("Getting categories")
                    output = get_categories()
                case "get_news":
                    st.toast("Getting news")
                    output = get_news(json.loads(tool.function.arguments).get("category", ""), json.loads(tool.function.arguments).get("search_term", ""))
                case "get_article":
                    st.toast("Getting article")
                    output = get_article(json.loads(tool.function.arguments)["article_id"])
                case "search_wiki":
                    st.toast("Searching Wikipedia")
                    output = search_wiki(json.loads(tool.function.arguments)["query"])
                case "wiki_summary":
                    st.toast("Getting data from Wikipedia")
                    output = ask_wiki(json.loads(tool.function.arguments)["wiki_term"])
                case "register_user_name":
                    st.toast("Recording user name")
                    output = register_user_name(json.loads(tool.function.arguments).get("name", ""))
                case "get_user_name":
                    st.toast("Loading user data")
                    output = get_user_name()
                case "register_user_interests":
                    st.toast("Saving preferences")
                    output = register_user_interests(json.loads(tool.function.arguments).get("interests", ""))
                case "get_user_interests":
                    st.toast("Loading preferences")
                    output = get_user_interests()
        except Exception as inst:
            st.error(str(inst))
            output = "ERROR CALLING A TOOL"
        tool_outputs.append({
            "tool_call_id": tool.id,
            "output": output
        })
    return tool_outputs


def wait_on_run(run, thread):
    done = False
    client = get_client()
    while not done:
        run = client.beta.threads.runs.retrieve(
            thread_id=thread.id,
            run_id=run.id,
        )
        if run.status == "completed":
            done = True
        if run.status == "requires_action":
            # Need to call a tool
            output = call_tools(run)
            run = client.beta.threads.runs.submit_tool_outputs(
                thread_id=thread.id,
                run_id=run.id,
                tool_outputs=output
            )
        elif run.status in ["cancelled"  "expired" "failed"]:
            # something wrong happened
            st.error("Run failed: %s" % run.status)
            st.stop()
        else:
            time.sleep(0.5)
    return run


def ask_model(prompt: str):
    client = get_client()
    assistant = get_assistant()
    thread = get_thread()
    
    log_msg(prompt)

    try:
        message = client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=prompt,
        )
    except:
        # Probably, a previous run is still working, maybe there was an error in calling a tool.
        st.error("Error while asking AI Assistant. Please reset the session to try again.")
        st.stop()
        pass
    
    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=assistant.id
    )
    wait_on_run(run, thread)
    messages = client.beta.threads.messages.list(
        thread_id=thread.id, order="asc", after=message.id
    )
    response = [m.content[0].text.value for m in messages]
    log_reply("\n".join(response))
    return response


def reset_session():
    log_action('RESET SESSION')
    for key in st.session_state:
        del st.session_state[key]
    client = get_client()
    # assistant = get_assistant()
    thread = get_thread()
    # cancel all runs
    for run in client.beta.threads.runs.list():
        client.beta.threads.runs.cancel(run_id=run.id, thread_id=thread.id)
    st.rerun()


def register_user_name(name = ""):
    pincode = st.session_state.get('pincode')
    if pincode:
        db = get_db()
        users_collection = db[USERS]
        users_collection.update_one({"pincode": pincode}, {"$set": {"name": name}}, upsert=True)
    return "Registered"


def get_user_name() -> str:
    pincode = st.session_state.get('pincode')
    if pincode:
        db = get_db()
        users_collection = db[USERS]
        user_data = users_collection.find_one({"pincode": pincode})
        if user_data:
            return user_data.get('name', '')
    return ""


def register_user_interests(interests = ""):
    pincode = st.session_state.get('pincode')
    if pincode:
        db = get_db()
        users_collection = db[INTERESTS]
        users_collection.update_one({"pincode": pincode}, {"$set": {"interests": interests}}, upsert=True)
    return "Registered"


def get_user_interests() -> str:
    pincode = st.session_state.get('pincode')
    if pincode:
        db = get_db()
        users_collection = db[INTERESTS]
        user_data = users_collection.find_one({"pincode": pincode})
        if user_data:
            return user_data.get('interests', '')
    return ""


def get_categories():
    # get a list of possible categories for news
    db = get_db()
    news_collection = db[NEWS]
    return "\n".join(news_collection.distinct('category'))


def get_news(category="", search_term=""):
    db = get_db()
    news_collection = db[NEWS]
    filters = {}
    if category:
        filters['category'] = category
    elif search_term:
        filters["$text"] = {"$search": search_term}
    entries = news_collection.find(filters).sort({"date": pymongo.DESCENDING}).limit(20)

    news_entries = []
    for entry in entries:
        news_entries.append(
            "{title}: {summary}. Article ID: {id}".format(**entry)
        )
    return "\n".join(news_entries)


# @st.cache_data(show_spinner="Getting article..", ttl=600)
def get_article(article_id: str):
    url = "https://www.vg.no/irisx/v1/articles/%s" % article_id.strip()
    a = requests.get(url)
    # check status
    article = a.json()
    if "components" not in article:
        return "ERROR GETTING ARTICLE"
    article_text = "\n".join([comp["text"]["value"] for comp in article["components"] if comp["type"] == "text"])
    return article_text


# @st.cache_data(show_spinner="Searching..", ttl=3600)
def search_wiki(query: str) -> str:
    results = wikipedia.search(query)
    return ", ".join(results)


# @st.cache_data(show_spinner="Getting more information..", ttl=3600)
def ask_wiki(query:str) -> str:
    try:
        s = wikipedia.summary(query)
    except wikipedia.PageError:
        s = "NO DATA FOUND"
    except wikipedia.DisambiguationError:
        s = "Encountered Disambiguation Error"
    except:
        s = "Unhandeled Error Occured"
    return s


# Main page
with st.container():
    # Login
    if "pincode" not in st.session_state:
        pincode = st.number_input("Pincode", min_value=1, max_value=9999, step=1, format="%d")
        if pincode not in pincodes:
            st.warning("Please enter correct pincode")
            st.stop()
        else:
            st.session_state["pincode"] = pincode
            # get user name
            st.session_state['user_name'] = get_user_name()
            st.rerun()

    is_user_introduced = False
    if "messages" not in st.session_state:
        intro_message = INTRO_MSG
        if not st.session_state.get('user_name'):
            intro_message += INTRO_USER_NAME
        else:
            intro_message = RETURN_MSG % st.session_state.get('user_name', '')
            is_user_introduced = True
        st.session_state["messages"] = [{"role": "assistant", "content": intro_message}]

    for msg in st.session_state["messages"]:
        st.chat_message(msg["role"]).write(msg["content"])

    if prompt := st.chat_input():

        client = get_client()
        st.session_state["messages"].append({"role": "user", "content": prompt})
        
        with st.spinner('Working...'):
            response = ask_model(prompt)
        for answer in response:
            st.session_state["messages"].append({"role": "assistant", "content": answer})
        st.rerun()
