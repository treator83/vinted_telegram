from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

HEADLESS = os.getenv("HEADLESS", "False").lower() == "true"

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

SEARCH_URLS = [
    os.getenv("SEARCH_URL_1"),
    os.getenv("SEARCH_URL_2"),
    os.getenv("SEARCH_URL_3"),
    os.getenv("SEARCH_URL_4"),
]

SEARCH_URLS = [u for u in SEARCH_URLS if u]

KEYWORDS = [
    k.strip().lower()
    for k in os.getenv("KEYWORDS", "").split(",")
    if k.strip()
]

MAX_PRICE = float(os.getenv("MAX_PRICE", "999999"))

SIZES = [
    s.strip()
    for s in os.getenv("SIZES", "").split(",")
    if s.strip()
]

CONDITIONS = [
    c.strip().lower()
    for c in os.getenv("CONDITIONS", "").split(",")
    if c.strip()
]