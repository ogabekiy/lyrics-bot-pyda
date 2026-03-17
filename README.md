# Lyrics Search Bot

This project is a simple Telegram bot written in Python. Users send a song query, the bot places it into a queue, and one worker fetches lyrics from Genius with a delay between requests so the API is not hit too aggressively.

## Features

- Uses `lyricsgenius` to search Genius lyrics
- Cleans common Genius boilerplate so users see the actual lyric lines
- Queues incoming requests with `asyncio.Queue`
- Processes one request at a time with a configurable delay
- Stores search history locally in SQLite
- Stores favorite songs locally in SQLite
- Stores users locally with Telegram `id`, `username`, and `name`
- Supports `/history [page]`, `/favorites [page]`, and `/unfavorite <id>`
- Sends long lyrics back in multiple Telegram messages when needed
- Keeps only a single like button under the lyric message

## Setup

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy the environment template and fill in your tokens:

```bash
cp .env.example .env
```

Required values:

- `TELEGRAM_BOT_TOKEN`: your Telegram bot token from BotFather
- `GENIUS_ACCESS_TOKEN`: your Genius API access token
- `REQUEST_DELAY_SECONDS`: seconds to wait after each Genius request

## Data Storage

The bot creates a local SQLite database file named `bot_data.db` in the project folder. It stores users, search history, and favorites.

## Run

```bash
python bot.py
```

## How It Works

- A user sends any text message to the bot
- The message is added to the queue
- The bot sends a simple "working on it" reply
- A background worker pulls one request at a time
- The worker searches Genius and sends the lyrics back to the same chat
- The worker cleans common Genius intro text before sending the lyrics
- The bot stores the search in SQLite for `/history`
- Users can like/unlike with the heart button under the lyric message
- Favorites and history are opened with commands
- After each request, the worker sleeps for the configured delay
