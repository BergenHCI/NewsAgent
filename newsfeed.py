import feedparser
import pymongo
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import urllib
import dateparser
import toml
import datetime

# Use VG since they got API, got it from https://loop24.no/loopsign/rss-feeds/
# https://www.vg.no/rss/feed/?categories=&keywords=politik&format=rss&limit=10
RSS_FEED = "https://www.vg.no/rss/feed/?" #  "https://www.nrk.no/toppsaker.rss"

NEWS = "newsfeed"

secrets = toml.load(".streamlit/secrets.toml")

def get_mongo() -> MongoClient:
    uri = "mongodb+srv://{db_user}:{db_pass}@{db_host}/?retryWrites=true&w=majority&appName=AtlasCluster".format(
        db_user = secrets['DB_USER'], db_pass = secrets['DB_PASSWORD'], db_host = secrets['DB_HOST']
    )
    # Create a new client and connect to the server
    mongo_client = MongoClient(uri, server_api=ServerApi('1'))
    return mongo_client


def get_news_from_rss(latest_date):
    rss_feed_url = RSS_FEED
    params = {"limit": 100}
    feed = feedparser.parse(rss_feed_url + urllib.parse.urlencode(params))
    # print("Getting: ", rss_feed_url + urllib.parse.urlencode(params))
    news = []
    for entry in feed.entries:
        entry_date = dateparser.parse(entry['published']).replace(tzinfo=None)
        if entry_date <= latest_date:
            # not news anymore, and we most likely have it in DB
            continue
        news.append({
            'id': entry['id'].split("/")[-1],
            'source': "VG",
            'title': entry['title'],
            'summary': entry.get('summary'),
            'category': entry['category'],
            'date': entry_date
        })
    return news


def update_news():
    # get the last news record
    mongo = get_mongo()
    db = mongo[secrets["DB_NAME"]]
    news_collection = db[NEWS]
    # search for news
    # save only those that were published later than the given date
    latest_record = list(news_collection.find({}).sort({"date": pymongo.DESCENDING}).limit(1))
    if len(latest_record) > 0:
        latest_date = latest_record[0].get("date").replace(tzinfo=None)
    else:
        # 14 days of news by default
        latest_date =(datetime.datetime.now() - datetime.timedelta(14)).replace(tzinfo=None)
    news = get_news_from_rss(latest_date)
    # save to mongo
    if news:
        news_collection.insert_many(news)
        news_collection.create_index([('title', 'text'), ('summary', 'text')])

if __name__ == "__main__":
    update_news()
