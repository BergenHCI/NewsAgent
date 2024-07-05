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

OsloTZ = pytz.timezone('Europe/Oslo')

# Use VG since they got API, got it from https://loop24.no/loopsign/rss-feeds/
NRK_RSS_FEED = "https://www.vg.no/rss/feed/" #  "https://www.nrk.no/toppsaker.rss"
GPT_MODEL = "gpt-3.5-turbo"


ASSISTANT_1_NAME = 'Assistant 1'
ASSISTANT_2_NAME = 'Assistant 2'


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


@st.cache_resource(ttl=360)
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
        'pincode': st.session_state.get('pincode', ''),
        'assistant_id': st.session_state.get('assistant_id', ''),
        'assistant': st.session_state.get('assistant', ''),
        'msg': msg,
        'is_user': is_user,
        'is_action': is_action
    })
    pass

def log_action(act:str):
    log_msg(act, True)

def log_reply(msg:str):
    log_msg(msg, is_user= False)

@st.cache_resource
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
    if not st.session_state["assistant_id"]:
        st.error("Please provide Assistant Id")
        st.stop()
    return client.beta.assistants.retrieve(st.session_state["assistant_id"])


def call_tools(run):
    tool_outputs = []
    for tool in run.required_action.submit_tool_outputs.tool_calls:
        output = ""
        log_action("Calling tool: %s" % tool.function.name)
        match tool.function.name:
            case "get_latest_news":
                st.toast("Getting news")
                output = get_news()
            case "get_article":
                st.toast("Getting article")
                output = get_article(json.loads(tool.function.arguments)["article_id"])
            case "search_wiki":
                st.toast("Searching")
                output = ", ".join(search_wiki(json.loads(tool.function.arguments)["query"]))
            case "wiki_summary":
                st.toast("Getting data from wikipedia")
                output = ask_wiki(json.loads(tool.function.arguments)["wiki_term"])
        tool_outputs.append({
            "tool_call_id": tool.id,
            "output": output
        })
    return tool_outputs


def wait_on_run(run, thread):
    done = False
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
        assistant_id=assistant.id,
        model=GPT_MODEL
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


@st.cache_data(show_spinner="Getting the news..", ttl=60)
def get_feed():
    feed = feedparser.parse(NRK_RSS_FEED)
    return feed


def get_news():
    feed = get_feed()
    news = []
    for entry in feed.entries:
        # parse entry id
        # entry['id']
        entry_id = entry['id'].split("/")[-1]
        n_entry = "%s %s Article ID: %s" % (entry['title'], entry['summary'], entry_id)
        news.append(n_entry)
    return "\n".join(news)


@st.cache_data(show_spinner="Getting article..", ttl=60)
def get_article(article_id: str):
    url = "https://www.vg.no/irisx/v1/articles/%s" % article_id.strip()
    a = requests.get(url)
    # check status
    article = a.json()
    article_text = "\n".join([comp["text"]["value"] for comp in article["components"] if comp["type"] == "text"])
    return article_text


@st.cache_data(show_spinner="Searching..", ttl=360)
def search_wiki(query: str) -> list[str]:
    return wikipedia.search(query)


@st.cache_data(show_spinner="Getting more information..", ttl=360)
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
            st.rerun()
    
    # Choose assistant
    if "assistant_id" not in st.session_state:
        with st.container(border=True):
            assist_button_1 = st.button(ASSISTANT_1_NAME)
            assist_button_2 = st.button(ASSISTANT_2_NAME)
            if assist_button_1:
                st.session_state["assistant_id"] = assistant_1_id
                st.session_state['assistant'] = ASSISTANT_1_NAME
                st.rerun()
            elif assist_button_2:
                st.session_state["assistant_id"] = assistant_2_id
                st.session_state['assistant'] = ASSISTANT_2_NAME
                st.rerun()
            st.stop()

    if "messages" not in st.session_state:
        st.session_state["messages"] = [{"role": "assistant", "content": "Hello. Ask whatever."}]

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
