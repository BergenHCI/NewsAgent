import pymongo
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import toml
import csv
import sys

# Mongo collection names
NEWS = "newsfeed"
LOGS = "logs"
USERS = "users"
INTERESTS = "interests"


secrets = toml.load(".streamlit/secrets.toml")

def get_mongo() -> MongoClient:
    uri = "mongodb+srv://{db_user}:{db_pass}@{db_host}/?retryWrites=true&w=majority&appName=AtlasCluster".format(
        db_user = secrets['DB_USER'], db_pass = secrets['DB_PASSWORD'], db_host = secrets['DB_HOST']
    )
    # Create a new client and connect to the server
    mongo_client = MongoClient(uri, server_api=ServerApi('1'))
    return mongo_client


def save_logs(pin_code:int):
    mongo = get_mongo()
    db = mongo[secrets["DB_NAME"]]
    logs_collection = db[LOGS]
    logs = list(logs_collection.find({"pincode": pin_code}))
    if not logs:
        print("No logs found")
        return
    
    keys = logs[0].keys()

    with open('logs/logs_%d.csv' % pin_code, 'w', newline='', encoding="utf8") as output_file:
        dict_writer = csv.DictWriter(output_file, keys)
        dict_writer.writeheader()
        dict_writer.writerows(logs)
    print("Saved")


if __name__ == "__main__":
    if sys.argv:
        save_logs(int(sys.argv[-1]))

