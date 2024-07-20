import streamlit as st
from openai import OpenAI, RateLimitError
import backoff
import feedparser
import wikipedia
import time, datetime
import pytz
import requests
import json
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import urllib

OsloTZ = pytz.timezone('Europe/Oslo')


# Use VG since they got API, got it from https://loop24.no/loopsign/rss-feeds/
# https://www.vg.no/rss/feed/?categories=&keywords=politik&format=rss&limit=10
RSS_FEED = "https://www.vg.no/rss/feed/" #  "https://www.nrk.no/toppsaker.rss"
# GPT_MODEL = "gpt-3.5-turbo"


ASSISTANT_1_NAME = 'Assistant 1'
ASSISTANT_2_NAME = 'Assistant 2'


INTRO_MSG = """Hei!
Jeg er Falk, din nye nyhetsassistent fra VG.
Min oppgave er å holde deg oppdatert på nyhetene fra VG gjennom en engasjerende chat-samtale. Jeg håper å skape meningsfulle diskusjoner og gi deg innsikt i aktuelle hendelser.
Jeg oppfordrer deg til å stille spørsmål om nyhetene, dele dine meninger og tanker, eller foreslå annet innhold du er nysgjerrig på. Jeg er her for å hjelpe deg med å holde deg oppdatert og engasjert.
Nyhetene jeg deler, er basert på VG sine artikler. Hvis du har spørsmål om noe som ikke dekkes i artiklene, bruker jeg Wikipedia for å finne svar. La oss sammen utforske de siste nyhetene og ha innsiktsfulle samtaler om hva som skjer i verden!
La oss bli litt bedre kjent før vi kommer i gang med nyhetene."""

INTRO_USER_NAME = " Hva vil du at jeg skal kalle deg?"


VG_CATEGORIES = {
    "Film": 1097,
    "Books": 1099,
    "Music": 1098,
    "TV": 1100,
    "Opinions": 1071,
    "Sports": 1072,
    "Football": 1073,
    "Handball": 1074,
    "Ice Hockey": 1075,
    "Cross-country Skiing": 1076,
    "Cycling": 1077,
    "Motorsports": 1079,
    "Athletics": 1080,
    "Ski Jumping": 1081,
    "Biathlon": 1081
}


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


def log_msg(msg:str, is_action = False, is_user= True):
    mongo = get_mongo()
    db = mongo[st.secrets["DB_NAME"]]
    logs_collection = db['logs']
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
        match tool.function.name:
            case "get_news":
                st.toast("Getting news")
                output = get_news(json.loads(tool.function.arguments).get("category", ""))
            case "get_article":
                st.toast("Getting article")
                output = get_article(json.loads(tool.function.arguments)["article_id"])
            case "search_wiki":
                st.toast("Searching")
                output = ", ".join(search_wiki(json.loads(tool.function.arguments)["query"]))
            case "wiki_summary":
                st.toast("Getting data from wikipedia")
                output = ask_wiki(json.loads(tool.function.arguments)["wiki_term"])
            case "register_user_name":
                st.toast("Recording user name")
                output = register_user_name(json.loads(tool.function.arguments).get("name", ""))
            case "get_user_name":
                st.toast("Loading user data")
                output = get_user_name()
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


def ask_model(message: str):
    client = get_client()
    assistant = get_assistant()
    thread = get_thread()
    
    log_msg(message)

    message = client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=message,
    )
    
    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=assistant.id #,
        # model=GPT_MODEL
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
        mongo = get_mongo()
        db = mongo[st.secrets["DB_NAME"]]
        users_collection = db['users']
        users_collection.update_one({"pincode": pincode}, {"$set": {"name": name}}, upsert=True)
    return "Registered"


def get_user_name() -> str:
    pincode = st.session_state.get('pincode')
    if pincode:
        mongo = get_mongo()
        db = mongo[st.secrets["DB_NAME"]]
        users_collection = db['users']
        user_data = users_collection.find_one({"pincode": pincode})
        if user_data:
            return user_data.get('name', '')
    return ""

# @st.cache_data(show_spinner="Getting the news..", ttl=600)
def get_feed(category=""):
    rss_feed_url = RSS_FEED
    params = {"limit": 20}
    if category:
        category_key = VG_CATEGORIES.get(category)
        if category_key:
            params["categories"] = category_key
    feed = feedparser.parse(rss_feed_url + urllib.parse.urlencode(params))
    return feed


def get_news(category=""):
    feed = get_feed(category)
    news = []
    for entry in feed.entries:
        # parse entry id
        # entry['id']
        entry_id = entry['id'].split("/")[-1]
        n_entry = "%s %s Article ID: %s" % (entry['title'], entry['summary'], entry_id)
        news.append(n_entry)
    return "\n".join(news)


# @st.cache_data(show_spinner="Getting article..", ttl=600)
def get_article(article_id: str):
    url = "https://www.vg.no/irisx/v1/articles/%s" % article_id.strip()
    a = requests.get(url)
    # check status
    article = a.json()
    article_text = "\n".join([comp["text"]["value"] for comp in article["components"] if comp["type"] == "text"])
    return article_text


# @st.cache_data(show_spinner="Searching..", ttl=3600)
def search_wiki(query: str) -> list[str]:
    return wikipedia.search(query)


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
            intro_message = "Hei %s" % st.session_state.get('user_name', '')
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
