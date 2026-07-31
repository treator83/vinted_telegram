Vinted Agent

Vinted Agent is a long-running Python service that monitors configured Vinted searches, filters listings, stores listing history in SQLite, and sends Telegram notifications for new listings and price drops.

The project is designed to run continuously on an Ubuntu server, including low-power mini PCs such as the Beelink Mini S12/S13.

What the application does

For every search defined in searches.json, Vinted Agent:

Opens the Vinted search page in Chromium using Selenium.

Accepts the cookie banner once per browser session.

Extracts listing information from the search results.

Applies search-specific filters:

keywords

maximum price

sizes

conditions

Checks whether each listing already exists in the SQLite database.

Saves new listings.

Detects price changes.

Records price history.

Sends Telegram notifications for:

new listings

price drops

Repeats the process after the configured interval.

Main features

Multiple Vinted searches

Reusable Chromium session

Automatic browser restart after recoverable failures

One-time cookie-banner handling

Listing filtering by search

SQLite storage

Database schema upgrades

Price-history tracking

Telegram photo notifications

Telegram text fallback when an image cannot be sent

HTTP retry handling

Rotating application logs

Graceful shutdown with Ctrl+C, SIGINT, or SIGTERM

Automated tests for models, filters, search loading, and database behavior

Ubuntu and systemd-friendly design

Project structure

vinted-agent/
├── browser.py             Chromium lifecycle and navigation
├── config.py              Environment-based configuration
├── database.py            SQLite listings and price history
├── filters.py             Search-specific filtering rules
├── logger.py              Console and rotating-file logging
├── main.py                Long-running application orchestration
├── models.py              Listing model and price parsing
├── scraper.py             Vinted page interaction and extraction
├── search.py              Search configuration model
├── search_manager.py      searches.json loading and validation
├── telegram_client.py     Telegram Bot API notifications
├── searches.json          Vinted searches and filters
├── test.py                Automated core tests
├── requirements.txt       Python runtime dependencies
├── .env.example           Example environment configuration
├── data/                  SQLite database files
└── logs/                  Rotating application logs

Requirements

Python 3.12 or newer

Chromium or Google Chrome

A compatible ChromeDriver, or Selenium Manager support

Telegram bot token

Telegram chat ID

Installation

Clone the repository:

git clone https://github.com/treator83/vinted_telegram.git
cd vinted_telegram

Create and activate a virtual environment:

python3 -m venv .venv
source .venv/bin/activate

Install dependencies:

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Configuration

Copy the example environment file:

cp .env.example .env

Edit .env:

BOT_TOKEN=replace_with_your_bot_token
CHAT_ID=replace_with_your_chat_id

CHECK_INTERVAL=60
HEADLESS=true

CHROME_BINARY=
CHROMEDRIVER_PATH=

Environment variables

Variable

Description

BOT_TOKEN

Telegram bot token

CHAT_ID

Telegram destination chat ID

CHECK_INTERVAL

Seconds to wait between complete monitoring cycles

HEADLESS

Run Chromium without a visible window

CHROME_BINARY

Optional custom Chromium or Chrome executable path

CHROMEDRIVER_PATH

Optional custom ChromeDriver path

Do not commit the real .env file. It contains secrets.

Search configuration

Searches are defined in searches.json.

Example:

[
  {
    "id": "rst_boots",
    "name": "RST Boots",
    "url": "https://www.vinted.co.uk/catalog?search_text=RST%20boots&order=newest_first&page=1",
    "max_price": 80,
    "keywords": [
      "rst"
    ],
    "sizes": [
      "9",
      "10"
    ],
    "conditions": [
      "Very Good",
      "Good"
    ]
  }
]

Search fields

Field

Required

Description

id

Yes

Unique identifier for the search

name

Yes

Human-readable name used in logs and Telegram

url

Yes

Complete Vinted search URL

max_price

No

Maximum listing price

keywords

No

At least one keyword must appear in the title or subtitle

sizes

No

At least one configured size must appear in the subtitle

conditions

No

At least one configured condition must appear in the subtitle

Each search ID must be unique.

Running the application

Start Vinted Agent:

python main.py

Stop it gracefully:

Ctrl+C

The application finishes the current operation, closes Chromium, closes the Telegram HTTP session, closes SQLite, and exits.

Database

The default database is:

data/listings.db

The database stores:

listing ID

search ID and name

title and subtitle

displayed price and total price

current and previous numeric prices

listing URL

image URL

first-seen timestamp

last-seen timestamp

Price changes are also written to the price_history table.

SQLite WAL mode is enabled for file-based databases to improve reliability and reduce locking during long-running use.

Telegram notifications

New listing

A new-listing message includes:

search name

listing title

listing information

item price

total price

listing URL

listing photo when available

Price drop

A price-drop message includes:

previous price

current price

amount saved

listing details

listing URL

listing photo when available

When a photo cannot be downloaded or uploaded, the application falls back to a text message.

Logging

Logs are written to:

logs/vinted.log

The log file rotates automatically when it reaches its configured maximum size. Older log files are retained as numbered backups.

When running under systemd, messages are also available through the service journal.

Tests

Run all automated tests:

python -m unittest -v test.py

The current test suite covers:

price parsing

listing numeric price calculation

keyword filters

price filters

size matching

condition matching

search JSON validation

duplicate search IDs

SQLite inserts

duplicate prevention

listing lookup

search counts

price updates

price history

database clearing

Monitoring cycle

A normal cycle follows this flow:

Load searches
    ↓
Start or reuse Chromium
    ↓
Open search page
    ↓
Accept cookies when required
    ↓
Extract listing cards
    ↓
Apply search filters
    ↓
Check SQLite
    ↓
Save new listings
    ↓
Record price changes
    ↓
Send Telegram notifications
    ↓
Log cycle statistics
    ↓
Sleep for CHECK_INTERVAL seconds

Failure of one listing does not stop the remaining listings. Failure of one search does not stop the remaining searches. Recoverable browser navigation failures trigger a fresh Chromium session.

Security

Keep .env private.

Never commit the Telegram bot token.

Do not commit live database files.

Do not run the service as root.

Use a dedicated Linux user for production deployment.

Restrict access to the project and data directories.

Production target

The intended production environment is:

Ubuntu Server 24.04 LTS

Python 3.12+

Headless Chromium

SQLite

systemd

Intel N95 or N150 mini PC

4 GB RAM minimum, 8 GB recommended

The service processes searches sequentially and reuses one Chromium instance to keep CPU and memory usage low.

Current status

The core monitoring application is implemented and tested.

Completed areas:

browser lifecycle

scraper

listing model

filters

database

price history

Telegram notifications

search loading

configuration

logging

long-running service loop

automated core tests

Planned deployment work:

Ubuntu installation script

systemd service file

update script

backup procedure

Disclaimer

This project is an independent monitoring tool and is not affiliated with, endorsed by, or operated by Vinted.

Use it responsibly and ensure that your use complies with Vinted's terms, local laws, and applicable rate limits.