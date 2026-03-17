import asyncio
import html
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import lyricsgenius
from dotenv import load_dotenv
from requests.exceptions import HTTPError
from telegram.error import TimedOut
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


MAX_MESSAGE_LENGTH = 4096
LYRICS_DATABASE_PATH = "bot_data.db"
PAGE_SIZE = 5
CALLBACK_HEART_PREFIX = "heart:"
COMMANDS_FOOTER = "\n\n<i>Commands: /history , /favorites , /unfavorite &lt;id&gt;</i>"


class GeniusTokenError(Exception):
    pass


async def safe_reply_text(message, text: str, **kwargs) -> None:
    for attempt in range(2):
        try:
            await message.reply_text(text, **kwargs)
            return
        except TimedOut:
            if attempt == 1:
                raise
            await asyncio.sleep(1)


async def safe_send_message(bot, chat_id: int, text: str, **kwargs) -> None:
    for attempt in range(2):
        try:
            await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            return
        except TimedOut:
            if attempt == 1:
                raise
            await asyncio.sleep(1)


@dataclass
class UserRecord:
    telegram_user_id: int
    username: Optional[str]
    first_name: Optional[str]


@dataclass
class SearchResultRecord:
    telegram_user_id: int
    query: str
    song_title: Optional[str]
    artist_name: Optional[str]
    status: str


@dataclass
class FavoriteRecord:
    telegram_user_id: int
    song_title: str
    artist_name: str


class UserStore:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self._initialize()

    def _initialize(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    query_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS search_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    query TEXT NOT NULL,
                    song_title TEXT,
                    artist_name TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (telegram_user_id) REFERENCES users (telegram_user_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS favorites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    song_title TEXT NOT NULL,
                    artist_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(telegram_user_id, song_title, artist_name),
                    FOREIGN KEY (telegram_user_id) REFERENCES users (telegram_user_id)
                )
                """
            )

    def ensure_user(self, user_record: UserRecord) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO users (
                    telegram_user_id,
                    username,
                    first_name,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    updated_at = excluded.updated_at
                """,
                (
                    user_record.telegram_user_id,
                    user_record.username,
                    user_record.first_name,
                    timestamp,
                    timestamp,
                ),
            )

    def increment_query_count(self, telegram_user_id: int) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE users
                SET query_count = query_count + 1,
                    updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), telegram_user_id),
            )

    def record_search(self, result: SearchResultRecord) -> int:
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO search_history (
                    telegram_user_id,
                    query,
                    song_title,
                    artist_name,
                    status,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.telegram_user_id,
                    result.query,
                    result.song_title,
                    result.artist_name,
                    result.status,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return int(cursor.lastrowid)

    def get_user_history_page(self, telegram_user_id: int, limit: int, offset: int) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT query, song_title, artist_name, status, created_at
                FROM search_history
                WHERE telegram_user_id = ?
                ORDER BY id DESC
                LIMIT ?
                OFFSET ?
                """,
                (telegram_user_id, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_history(self, telegram_user_id: int) -> int:
        with sqlite3.connect(self.database_path) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM search_history WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()[0]

    def add_favorite(self, favorite: FavoriteRecord) -> bool:
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO favorites (
                    telegram_user_id,
                    song_title,
                    artist_name,
                    created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    favorite.telegram_user_id,
                    favorite.song_title,
                    favorite.artist_name,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cursor.rowcount > 0

    def remove_favorite(self, favorite_id: int, telegram_user_id: int) -> bool:
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.execute(
                "DELETE FROM favorites WHERE id = ? AND telegram_user_id = ?",
                (favorite_id, telegram_user_id),
            )
            return cursor.rowcount > 0

    def remove_favorite_by_song(self, telegram_user_id: int, song_title: str, artist_name: str) -> bool:
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.execute(
                """
                DELETE FROM favorites
                WHERE telegram_user_id = ? AND song_title = ? AND artist_name = ?
                """,
                (telegram_user_id, song_title, artist_name),
            )
            return cursor.rowcount > 0

    def get_favorites_page(self, telegram_user_id: int, limit: int, offset: int) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, song_title, artist_name, created_at
                FROM favorites
                WHERE telegram_user_id = ?
                ORDER BY id DESC
                LIMIT ?
                OFFSET ?
                """,
                (telegram_user_id, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_favorites(self, telegram_user_id: int) -> int:
        with sqlite3.connect(self.database_path) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM favorites WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()[0]

    def is_favorite(self, telegram_user_id: int, song_title: str, artist_name: str) -> bool:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM favorites
                WHERE telegram_user_id = ? AND song_title = ? AND artist_name = ?
                LIMIT 1
                """,
                (telegram_user_id, song_title, artist_name),
            ).fetchone()
        return row is not None

    def get_search_result(self, search_id: int, telegram_user_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT song_title, artist_name, status
                FROM search_history
                WHERE id = ? AND telegram_user_id = ?
                """,
                (search_id, telegram_user_id),
            ).fetchone()
        return dict(row) if row else None


@dataclass
class LyricsRequest:
    chat_id: int
    telegram_user_id: int
    query: str


class LyricsSearchQueue:
    def __init__(self, genius_client: lyricsgenius.Genius, delay_seconds: float) -> None:
        self.genius_client = genius_client
        self.delay_seconds = delay_seconds
        self.queue: asyncio.Queue[LyricsRequest] = asyncio.Queue()

    async def enqueue(self, request: LyricsRequest) -> None:
        await self.queue.put(request)

    async def worker(self, application: Application) -> None:
        while True:
            request = await self.queue.get()
            try:
                await self._process_request(application, request)
            except GeniusTokenError:
                logging.exception("Genius token is invalid or expired")
                await safe_send_message(
                    application.bot,
                    chat_id=request.chat_id,
                    text="Genius token noto'g'ri yoki eskirgan.",
                )
            except Exception:
                logging.exception("Unexpected error while processing lyric request")
                await safe_send_message(
                    application.bot,
                    chat_id=request.chat_id,
                    text="Lyrics qidirishda xatolik bo'ldi. Keyinroq urinib ko'ring.",
                )
            finally:
                self.queue.task_done()
                await asyncio.sleep(self.delay_seconds)

    async def _process_request(self, application: Application, request: LyricsRequest) -> None:
        await application.bot.send_chat_action(chat_id=request.chat_id, action=ChatAction.TYPING)

        song = await asyncio.to_thread(self._search_song, request.query)
        user_store: UserStore = application.bot_data["user_store"]

        if song is None or not song.lyrics:
            await asyncio.to_thread(
                user_store.record_search,
                SearchResultRecord(
                    telegram_user_id=request.telegram_user_id,
                    query=request.query,
                    song_title=None,
                    artist_name=None,
                    status="not_found",
                ),
            )
            await safe_send_message(
                application.bot,
                chat_id=request.chat_id,
                text=f"Lyrics topilmadi: {request.query}",
            )
            return

        cleaned_lyrics = clean_lyrics(song.lyrics)
        if not cleaned_lyrics:
            await asyncio.to_thread(
                user_store.record_search,
                SearchResultRecord(
                    telegram_user_id=request.telegram_user_id,
                    query=request.query,
                    song_title=getattr(song, "title", None),
                    artist_name=getattr(song, "artist", None),
                    status="empty_after_cleanup",
                ),
            )
            await safe_send_message(
                application.bot,
                chat_id=request.chat_id,
                text=f"Lyrics tozalangandan keyin bo'sh chiqdi: {request.query}",
            )
            return

        search_id = await asyncio.to_thread(
            user_store.record_search,
            SearchResultRecord(
                telegram_user_id=request.telegram_user_id,
                query=request.query,
                song_title=getattr(song, "title", None),
                artist_name=getattr(song, "artist", None),
                status="success",
            ),
        )

        title = getattr(song, "title", request.query) or request.query
        artist = getattr(song, "artist", "Unknown artist") or "Unknown artist"
        caption = build_song_caption(title, artist)
        lyrics_chunks = split_text(
            cleaned_lyrics,
            MAX_MESSAGE_LENGTH - len(caption) - len(COMMANDS_FOOTER),
        )
        is_favorite = await asyncio.to_thread(
            user_store.is_favorite,
            request.telegram_user_id,
            title,
            artist,
        )
        reply_markup = build_like_markup(search_id, is_favorite)

        for index, chunk in enumerate(lyrics_chunks):
            if index == 0:
                message = f"{caption}{chunk}"
            else:
                message = chunk

            if index == len(lyrics_chunks) - 1:
                message = f"{message}{COMMANDS_FOOTER}"

            await safe_send_message(
                application.bot,
                chat_id=request.chat_id,
                text=message,
                parse_mode="HTML",
                reply_markup=reply_markup if index == len(lyrics_chunks) - 1 else None,
            )

    def _search_song(self, query: str):
        try:
            search_results = self.genius_client.search_songs(query, per_page=10)
            selected_hit = pick_best_hit(search_results, query)
            if selected_hit is not None:
                result = selected_hit.get("result", {})
                title = result.get("title")
                primary_artist = (result.get("primary_artist") or {}).get("name")
                if title and primary_artist:
                    return self.genius_client.search_song(title, primary_artist)

            return self.genius_client.search_song(query)
        except HTTPError as error:
            status_code = getattr(error, "errno", None)
            if status_code == 401:
                raise GeniusTokenError from error
            raise


def build_genius_client(access_token: str) -> lyricsgenius.Genius:
    return lyricsgenius.Genius(
        access_token,
        skip_non_songs=True,
        excluded_terms=["(Remix)", "(Live)"],
        remove_section_headers=True,
        timeout=15,
        retries=3,
    )


def build_song_caption(title: str, artist: str) -> str:
    return f"<b>{html.escape(title)}</b> - <i>{html.escape(artist)}</i>\n\n"


def build_like_markup(search_id: int, is_favorite: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❤️" if is_favorite else "🤍", callback_data=f"{CALLBACK_HEART_PREFIX}{search_id}")]]
    )


def clean_lyrics(raw_lyrics: str) -> str:
    text = raw_lyrics.replace("\r\n", "\n").strip()
    text = re.sub(r"^.*?Read More\s*", "", text, flags=re.IGNORECASE | re.DOTALL)
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    start_index = 0
    for index, line in enumerate(lines):
        if looks_like_lyrics_line(line):
            start_index = index
            break
    else:
        return text

    cleaned_lines = lines[start_index:]
    if cleaned_lines and is_trailing_embed_line(cleaned_lines[-1]):
        cleaned_lines = cleaned_lines[:-1]

    return "\n".join(cleaned_lines).strip()


def pick_best_hit(search_results: Any, query: str) -> Optional[Dict[str, Any]]:
    sections = search_results.get("sections") if isinstance(search_results, dict) else None
    if not sections:
        return None

    hits: List[Dict[str, Any]] = []
    for section in sections:
        hits.extend(section.get("hits", []))

    query_terms = tokenize(query)
    best_hit: Optional[Dict[str, Any]] = None
    best_score = float("-inf")

    for hit in hits:
        result = hit.get("result", {})
        title = str(result.get("title", ""))
        full_title = str(result.get("full_title", ""))
        artist = str((result.get("primary_artist") or {}).get("name", ""))
        url = str(result.get("url", ""))

        score = 0
        if not is_translation_hit(title, full_title, url):
            score += 100
        score += len(query_terms & tokenize(title)) * 8
        score += len(query_terms & tokenize(artist)) * 4

        if score > best_score:
            best_score = score
            best_hit = hit

    return best_hit


def tokenize(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", value.lower()))


def is_translation_hit(title: str, full_title: str, url: str) -> bool:
    haystack = f"{title} {full_title} {url}".lower()
    markers = (
        "translation",
        "translated",
        "traduction",
        "traducao",
        "traduccion",
        "traduzione",
        "ceviri",
        "çeviri",
        "перевод",
    )
    return any(marker in haystack for marker in markers)


def looks_like_lyrics_line(line: str) -> bool:
    normalized = line.lower()
    if normalized.startswith(("contributors", "translations", "lyrics", "embed")):
        return False
    if "read more" in normalized:
        return False
    if line.endswith("Lyrics") and len(line.split()) <= 8:
        return False
    if re.fullmatch(r"\d+\s*contributors?.*", normalized):
        return False
    return True


def is_trailing_embed_line(line: str) -> bool:
    return bool(re.fullmatch(r".*embed\b\d*", line.strip(), flags=re.IGNORECASE))


def split_text(text: str, limit: int) -> List[str]:
    escaped_text = html.escape(text)
    if len(escaped_text) <= limit:
        return [escaped_text]

    chunks: List[str] = []
    remaining_lines = [html.escape(line) for line in text.splitlines()]
    current_chunk = ""

    for line in remaining_lines:
        if len(line) > limit:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""
            chunks.extend(split_long_line(line, limit))
            continue

        next_chunk = f"{current_chunk}\n{line}" if current_chunk else line
        if len(next_chunk) > limit:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk = next_chunk

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def split_long_line(line: str, limit: int) -> List[str]:
    return [line[index : index + limit] for index in range(0, len(line), limit)]


def get_user_record(update: Update) -> Optional[UserRecord]:
    user = update.effective_user
    if user is None:
        return None

    return UserRecord(
        telegram_user_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )


async def persist_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[UserRecord]:
    user_record = get_user_record(update)
    if user_record is None:
        return None

    user_store: UserStore = context.application.bot_data["user_store"]
    await asyncio.to_thread(user_store.ensure_user, user_record)
    return user_record


def parse_page(args: List[str]) -> int:
    if not args:
        return 1

    try:
        return max(1, int(args[0]))
    except ValueError:
        return 1


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await persist_user(update, context)
    if update.message is None:
        return

    await safe_reply_text(
        update.message,
        "Qo'shiq nomini yuboring.\n\n"
        "Commands:\n"
        "/history \n"
        "/favorites \n"
        "/unfavorite <id>"
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_record = await persist_user(update, context)
    if update.message is None or user_record is None:
        return

    page = parse_page(context.args)
    offset = (page - 1) * PAGE_SIZE
    user_store: UserStore = context.application.bot_data["user_store"]
    total = await asyncio.to_thread(user_store.count_history, user_record.telegram_user_id)
    history = await asyncio.to_thread(user_store.get_user_history_page, user_record.telegram_user_id, PAGE_SIZE, offset)

    if not history:
        await safe_reply_text(update.message, "History bo'sh.")
        return

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [f"Recent searches | Page {page}/{total_pages}"]
    for item in history:
        title = item["song_title"] or item["query"]
        artist = item["artist_name"] or "Unknown artist"
        lines.append(f"- {title} - {artist}")
    await safe_reply_text(update.message, "\n".join(lines))


async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_record = await persist_user(update, context)
    if update.message is None or user_record is None:
        return

    page = parse_page(context.args)
    offset = (page - 1) * PAGE_SIZE
    user_store: UserStore = context.application.bot_data["user_store"]
    total = await asyncio.to_thread(user_store.count_favorites, user_record.telegram_user_id)
    favorites = await asyncio.to_thread(user_store.get_favorites_page, user_record.telegram_user_id, PAGE_SIZE, offset)

    if not favorites:
        await safe_reply_text(update.message, "Favorites bo'sh.")
        return

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [f"Favorites | Page {page}/{total_pages}"]
    for item in favorites:
        lines.append(f"{item['id']}. {item['song_title']} - {item['artist_name']}")
    lines.append("")
    lines.append("O'chirish: /unfavorite <id>")
    await safe_reply_text(update.message, "\n".join(lines))


async def unfavorite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_record = await persist_user(update, context)
    if update.message is None or user_record is None:
        return

    if not context.args:
        await safe_reply_text(update.message, "Foydalanish: /unfavorite <id>")
        return

    try:
        favorite_id = int(context.args[0])
    except ValueError:
        await safe_reply_text(update.message, "ID raqam bo'lishi kerak.")
        return

    user_store: UserStore = context.application.bot_data["user_store"]
    removed = await asyncio.to_thread(user_store.remove_favorite, favorite_id, user_record.telegram_user_id)
    await safe_reply_text(
        update.message,
        "Favorites'dan o'chirildi." if removed else "Bunday favorite topilmadi.",
    )


async def handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not update.message.text:
        return

    user_record = await persist_user(update, context)
    if user_record is None:
        return

    queue: LyricsSearchQueue = context.application.bot_data["lyrics_queue"]
    request = LyricsRequest(
        chat_id=update.effective_chat.id,
        telegram_user_id=user_record.telegram_user_id,
        query=update.message.text.strip(),
    )

    if not request.query:
        await safe_reply_text(update.message, "Qo'shiq nomini yuboring.")
        return

    user_store: UserStore = context.application.bot_data["user_store"]
    await asyncio.to_thread(user_store.increment_query_count, user_record.telegram_user_id)
    await queue.enqueue(request)
    await safe_reply_text(update.message, f"Qidirilmoqda: {request.query}...")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return

    user_record = await persist_user(update, context)
    if user_record is None or query.message is None:
        return

    if not query.data.startswith(CALLBACK_HEART_PREFIX):
        return

    search_id = int(query.data.replace(CALLBACK_HEART_PREFIX, "", 1))
    user_store: UserStore = context.application.bot_data["user_store"]
    search_result = await asyncio.to_thread(
        user_store.get_search_result,
        search_id,
        user_record.telegram_user_id,
    )

    if not search_result or search_result["status"] != "success":
        await query.answer("Like yangilanmadi")
        return

    song_title = search_result["song_title"] or "Unknown title"
    artist_name = search_result["artist_name"] or "Unknown artist"
    is_favorite = await asyncio.to_thread(
        user_store.is_favorite,
        user_record.telegram_user_id,
        song_title,
        artist_name,
    )

    if is_favorite:
        await asyncio.to_thread(
            user_store.remove_favorite_by_song,
            user_record.telegram_user_id,
            song_title,
            artist_name,
        )
        await query.message.edit_reply_markup(reply_markup=build_like_markup(search_id, False))
        await query.answer("Unlike qilindi")
        return

    await asyncio.to_thread(
        user_store.add_favorite,
        FavoriteRecord(
            telegram_user_id=user_record.telegram_user_id,
            song_title=song_title,
            artist_name=artist_name,
        ),
    )
    await query.message.edit_reply_markup(reply_markup=build_like_markup(search_id, True))
    await query.answer("Like qilindi")


async def post_init(application: Application) -> None:
    queue: LyricsSearchQueue = application.bot_data["lyrics_queue"]
    application.bot_data["queue_worker_task"] = asyncio.create_task(queue.worker(application))


async def post_shutdown(application: Application) -> None:
    worker_task = application.bot_data.get("queue_worker_task")
    if worker_task is not None:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled bot error", exc_info=context.error)


def main() -> None:
    load_dotenv()

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    genius_token = os.getenv("GENIUS_ACCESS_TOKEN")
    delay_seconds = float(os.getenv("REQUEST_DELAY_SECONDS", "3"))

    if not telegram_token or not genius_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN yoki GENIUS_ACCESS_TOKEN topilmadi.")

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=10.0,
    )

    application = (
        Application.builder()
        .token(telegram_token)
        .request(request)
        .get_updates_request(request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["lyrics_queue"] = LyricsSearchQueue(
        genius_client=build_genius_client(genius_token),
        delay_seconds=delay_seconds,
    )
    application.bot_data["user_store"] = UserStore(LYRICS_DATABASE_PATH)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("favorites", favorites_command))
    application.add_handler(CommandHandler("unfavorite", unfavorite_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_query))
    application.add_error_handler(error_handler)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        application.run_polling(drop_pending_updates=True)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
