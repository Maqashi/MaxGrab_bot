import asyncio
import base64
import html
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

import yt_dlp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import (
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
    CommandStart,
    IS_MEMBER,
    IS_NOT_MEMBER,
)
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeDefault,
    CallbackQuery,
    ChatMemberUpdated,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from telethon import Button as TgButton
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_USERNAME = "maxgrab_media_bot"

STANDARD_TG_SIZE_BYTES = 49 * 1024 * 1024   # 49 MB safe Bot API limit
MTPROTO_TG_SIZE_BYTES = 2000 * 1024 * 1024  # 2000 MB (2 GB) MTProto limit
MTPROTO_ENABLED = bool(API_ID and API_HASH)
MAX_TG_SIZE_BYTES = MTPROTO_TG_SIZE_BYTES if MTPROTO_ENABLED else STANDARD_TG_SIZE_BYTES
MAX_LIMIT_LABEL = "2 ГБ" if MTPROTO_ENABLED else "50 МБ"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" if (BASE_DIR / "data").is_dir() else BASE_DIR
DB_PATH = DATA_DIR / "maxgrab.db"
MTPROTO_SESSION_PATH = str(DATA_DIR / "maxgrab_mtproto")
mtproto_client: TelegramClient | None = None
COOKIES_PATH: str | None = None


def init_cookies_file() -> str | None:
    """Resolve cookies.txt and copy it to a writable /tmp file (Render /etc/secrets is read-only)."""
    writable_dst = Path(tempfile.gettempdir()) / "maxgrab_cookies.txt"

    candidates = [
        os.getenv("COOKIES_FILE", "").strip(),
        "/etc/secrets/cookies.txt",
        str(Path(__file__).resolve().parent / "cookies.txt"),
    ]
    raw_text: str | None = None
    source_label = ""

    for path_str in candidates:
        if path_str and Path(path_str).is_file() and Path(path_str).stat().st_size > 0:
            raw_text = Path(path_str).read_text(encoding="utf-8", errors="ignore")
            source_label = path_str
            break

    if raw_text is None:
        cookies_b64 = os.getenv("COOKIES_B64", "").strip()
        if cookies_b64:
            try:
                raw_text = base64.b64decode(cookies_b64).decode("utf-8", errors="ignore")
                source_label = "COOKIES_B64"
            except Exception as exc:
                logger.warning("[COOKIES] Не удалось декодировать COOKIES_B64: %s", exc)

    if not raw_text:
        logger.warning("[COOKIES] Файл cookies.txt НЕ найден (проверены /etc/secrets/cookies.txt, COOKIES_FILE, COOKIES_B64).")
        return None

    yt_lines = sum(1 for line in raw_text.splitlines() if "youtube.com" in line and not line.startswith("#"))
    if raw_text.lstrip().startswith("[") or raw_text.lstrip().startswith("{"):
        logger.error("[COOKIES] ОШИБКА ФОРМАТА (%s): похоже на JSON, а yt-dlp нужен формат Netscape!", source_label)
        return None

    writable_dst.write_text(raw_text, encoding="utf-8")
    logger.info("[COOKIES] Загружено из %s -> %s (строк youtube.com: %d)", source_label, writable_dst, yt_lines)
    return str(writable_dst)


def humanize_error(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return (
            "YouTube запросил авторизацию для этого видео. "
            "Админу нужно обновить файл <code>cookies.txt</code> на сервере."
        )
    if "[instagram:user]" in low:
        return (
            "Похоже, это ссылка на <b>профиль Instagram</b>, а не на видео.\n"
            "Отправь ссылку именно на конкретный <b>Reels</b> или <b>видео-пост</b>."
        )
    if "private video" in low or "login required" in low or "members-only" in low:
        return "Это видео приватное или доступно только после входа в аккаунт."
    if "requested format is not available" in low:
        return "Не удалось получить доступный формат видео. Попробуй выбрать другое качество или MP3."
    if "video unavailable" in low or "not available" in low:
        return "Видео недоступно, удалено или ограничено по региону."
    if "unsupported url" in low:
        return "Эта ссылка не поддерживается — проверь, что ссылка ведёт прямо на видео или трек."
    return f"<code>{html.escape(msg[:280])}</code>"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MaxGrabBot")

# Domains auto-detected in group chats without needing a /command
GROUP_MEDIA_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com",
    "vk.com",
    "vkvideo.ru",
    "soundcloud.com",
    "twitch.tv",
    "x.com",
    "twitter.com",
    "pinterest.",
    "pin.it",
    "reddit.com",
    "rutube.ru",
    "vimeo.com",
    "dailymotion.com",
)


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                mode TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                chat_type TEXT,
                added_at TEXT NOT NULL,
                last_active TEXT NOT NULL
            )
            """
        )


def track_user(user) -> None:
    if not user:
        return
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, joined_at, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_seen = excluded.last_seen
            """,
            (user.id, user.username or "", user.first_name or "", now, now),
        )


def track_chat(chat) -> None:
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO chats (chat_id, title, chat_type, added_at, last_active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                chat_type = excluded.chat_type,
                last_active = excluded.last_active
            """,
            (chat.id, chat.title or "Без названия", str(chat.type), now, now),
        )


def remove_chat(chat_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))


def list_tracked_chats() -> list[tuple[int, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT chat_id, title FROM chats ORDER BY last_active DESC LIMIT 30"
        ).fetchall()


def track_download(user_id: int, platform: str, mode: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO downloads (user_id, platform, mode, created_at) VALUES (?, ?, ?, ?)",
            (user_id, platform or "Web", mode, now),
        )


def get_stats_text() -> str:
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        new_today = conn.execute("SELECT COUNT(*) FROM users WHERE joined_at LIKE ?", (f"{today_prefix}%",)).fetchone()[0]
        active_today = conn.execute("SELECT COUNT(*) FROM users WHERE last_seen LIKE ?", (f"{today_prefix}%",)).fetchone()[0]
        total_chats = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        total_dl = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        dl_today = conn.execute("SELECT COUNT(*) FROM downloads WHERE created_at LIKE ?", (f"{today_prefix}%",)).fetchone()[0]
        top_platforms = conn.execute(
            "SELECT platform, COUNT(*) as cnt FROM downloads GROUP BY platform ORDER BY cnt DESC LIMIT 5"
        ).fetchall()

    platforms_block = ""
    if top_platforms:
        lines = [f"• {html.escape(p)}: <b>{c}</b>" for p, c in top_platforms]
        platforms_block = "\n\n🌐 <b>Топ площадок:</b>\n" + "\n".join(lines)

    return (
        f"📊 <b>Статистика @{BOT_USERNAME}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Всего пользователей: <b>{total_users}</b>\n"
        f"🆕 Новых сегодня: <b>{new_today}</b>\n"
        f"🟢 Активных сегодня: <b>{active_today}</b>\n"
        f"💬 Групповых чатов: <b>{total_chats}</b>\n\n"
        f"⬇ Всего скачиваний: <b>{total_dl}</b>\n"
        f"⚡ Скачиваний сегодня: <b>{dl_today}</b>"
        f"{platforms_block}"
    )


router = Router()
executor = ThreadPoolExecutor(max_workers=4)

url_store: Dict[str, dict] = {}
URL_REGEX = re.compile(r"https?://\S+")


def extract_url_from_message(message: Message, args: str | None = None) -> str | None:
    if args:
        m = URL_REGEX.search(args)
        if m:
            return m.group(0)
    if message.text:
        m = URL_REGEX.search(message.text)
        if m:
            return m.group(0)
    if message.caption:
        m = URL_REGEX.search(message.caption)
        if m:
            return m.group(0)
    if message.reply_to_message:
        rtxt = message.reply_to_message.text or message.reply_to_message.caption or ""
        m = URL_REGEX.search(rtxt)
        if m:
            return m.group(0)
    return None


def is_supported_group_url(url: str) -> bool:
    low = url.lower()
    return any(dom in low for dom in GROUP_MEDIA_DOMAINS)


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h > 0 else f"{m}:{sec:02d}"


def build_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить бота в чат",
                    url=f"https://t.me/{BOT_USERNAME}?startgroup=true",
                )
            ],
            [
                InlineKeyboardButton(text="🌐 Площадки", callback_data="ui:sites"),
                InlineKeyboardButton(text="💡 Как пользоваться", callback_data="ui:help"),
            ],
        ]
    )


def format_size(size_bytes: int | float | None) -> str:
    if not size_bytes or size_bytes <= 0:
        return ""
    mb = size_bytes / (1024 * 1024)
    if mb < 1:
        return f"~{size_bytes / 1024:.0f} КБ"
    return f"~{mb:.1f} МБ"


def _fmt_size_bytes(f: dict, duration: float) -> float:
    size = f.get("filesize") or f.get("filesize_approx")
    if size:
        return float(size)
    tbr = f.get("tbr") or f.get("vbr") or f.get("abr")
    if tbr and duration:
        return float(tbr) * 1000.0 / 8.0 * float(duration)
    return 0.0


def estimate_sizes(info: dict) -> tuple[Dict[str, float], Dict[str, str]]:
    formats = info.get("formats") or []
    duration = float(info.get("duration") or 0)

    audio_formats = [f for f in formats if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    best_audio_size = 0.0
    if audio_formats:
        best_audio = max(audio_formats, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        best_audio_size = _fmt_size_bytes(best_audio, duration)

    video_formats = [f for f in formats if f.get("vcodec") not in (None, "none")]

    def pick_video_size(max_h: int | None) -> float:
        candidates = video_formats
        if max_h is not None:
            filtered = [f for f in video_formats if (f.get("height") or 0) <= max_h]
            if filtered:
                candidates = filtered
        if not candidates:
            return float(info.get("filesize") or info.get("filesize_approx") or 0)
        best_v = max(candidates, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        v_size = _fmt_size_bytes(best_v, duration)
        if best_v.get("acodec") in (None, "none"):
            return v_size + best_audio_size
        return v_size

    mp3_size = duration * (192_000 / 8.0) if duration > 0 else best_audio_size

    raw = {
        "best": pick_video_size(None),
        "720": pick_video_size(720),
        "480": pick_video_size(480),
        "mp3": mp3_size,
    }
    formatted = {k: format_size(v) for k, v in raw.items()}
    return raw, formatted


def build_switch_keyboard(token: str, current_mode: str) -> InlineKeyboardMarkup:
    if current_mode == "mp3":
        btn = InlineKeyboardButton(text="🎬 Заменить на видео", callback_data=f"sw:{token}:best")
    else:
        btn = InlineKeyboardButton(text="🎵 Заменить на музыку (MP3)", callback_data=f"sw:{token}:mp3")
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


def build_format_keyboard(
    token: str,
    sizes: Dict[str, str] | None = None,
    raw_sizes: Dict[str, float] | None = None,
) -> InlineKeyboardMarkup:
    s = sizes or {}
    r = raw_sizes or {}

    def lbl(base: str, key: str) -> str:
        sz = s.get(key, "")
        over = r.get(key, 0) > MAX_TG_SIZE_BYTES
        prefix = "⚠️ " if over else ""
        return f"{prefix}{base} ({sz})" if sz else f"{prefix}{base}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=lbl("🎬 Макс.", "best"), callback_data=f"dl:{token}:best"),
                InlineKeyboardButton(text=lbl("🎬 720p", "720"), callback_data=f"dl:{token}:720"),
            ],
            [
                InlineKeyboardButton(text=lbl("🎬 480p", "480"), callback_data=f"dl:{token}:480"),
                InlineKeyboardButton(text=lbl("🎵 MP3", "mp3"), callback_data=f"dl:{token}:mp3"),
            ],
        ]
    )


def _base_ydl_opts() -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "js_runtimes": {"deno": {"path": None}, "node": {"path": None}},
        "remote_components": ["ejs:github"],
    }
    if COOKIES_PATH and Path(COOKIES_PATH).is_file():
        opts["cookiefile"] = COOKIES_PATH
    return opts


def extract_info_sync(url: str) -> dict:
    ydl_opts = {
        **_base_ydl_opts(),
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    raw_sizes, str_sizes = estimate_sizes(info)
    return {
        "title": info.get("title") or "Медиафайл",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or 0,
        "extractor": info.get("extractor_key") or info.get("extractor") or "Web",
        "sizes": str_sizes,
        "raw_sizes": raw_sizes,
    }


def download_media_sync(url: str, mode: str, temp_dir: Path) -> tuple[Path, dict]:
    outtmpl = str(temp_dir / "%(title).80s.%(ext)s")
    ydl_opts: dict = {
        **_base_ydl_opts(),
        "outtmpl": outtmpl,
        "windowsfilenames": True,
    }

    if mode == "mp3":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        if mode == "best":
            ydl_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        else:
            ydl_opts["format"] = (
                f"bestvideo[height<={mode}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={mode}]+bestaudio/best[height<={mode}]/best"
            )
        ydl_opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [
        p for p in temp_dir.iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl", ".tmp"))
    ]
    if not files:
        raise RuntimeError("Файл не был создан после загрузки")
    return files[0], info or {}


def store_url_entry(url: str, raw_sizes: dict | None = None, sizes: dict | None = None) -> str:
    token = uuid.uuid4().hex[:10]
    url_store[token] = {"url": url, "raw_sizes": raw_sizes or {}, "sizes": sizes or {}}
    if len(url_store) > 500:
        url_store.pop(next(iter(url_store)))
    return token


async def init_mtproto_uploader() -> None:
    global mtproto_client
    if not MTPROTO_ENABLED:
        logger.info("API_ID / API_HASH не заданы — активен стандартный лимит 50 МБ.")
        return
    try:
        client = TelegramClient(MTPROTO_SESSION_PATH, API_ID, API_HASH)
        await client.start(bot_token=BOT_TOKEN)
        mtproto_client = client
        logger.info("MTProto-загрузчик (Telethon) активен — лимит файлов увеличен до 2 ГБ!")
    except Exception as exc:
        logger.warning("Не удалось запустить MTProto-загрузчик (остаётся лимит 50 МБ): %s", exc)
        mtproto_client = None


async def send_media_smart(
    bot: Bot,
    target_chat_id: int,
    file_path: Path,
    info: dict,
    mode: str,
    token: str,
    caption: str,
    status_msg: Message,
    reply_to_message_id: int | None = None,
    fallback_username: str | None = None,
) -> None:
    """Send media via aiogram if <= 49 MB, or via Telethon MTProto up to 2 GB with live progress."""
    size = file_path.stat().st_size
    title_raw = (info.get("title") or file_path.stem)[:64]
    uploader_raw = info.get("uploader") or None
    duration = int(info.get("duration") or 0)
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)

    if size <= STANDARD_TG_SIZE_BYTES or mtproto_client is None:
        await status_msg.edit_text("📤 <b>Отправляю файл в чат...</b>")
        switch_kb = build_switch_keyboard(token, mode)
        input_file = FSInputFile(file_path, filename=file_path.name)
        if mode == "mp3":
            await bot.send_audio(
                chat_id=target_chat_id,
                audio=input_file,
                title=title_raw,
                performer=uploader_raw,
                caption=caption,
                reply_markup=switch_kb,
                reply_to_message_id=reply_to_message_id,
            )
        else:
            await bot.send_video(
                chat_id=target_chat_id,
                video=input_file,
                caption=caption,
                supports_streaming=True,
                reply_markup=switch_kb,
                reply_to_message_id=reply_to_message_id,
            )
        return

    # Large file (49 MB .. 2 GB) -> MTProto upload via Telethon!
    size_mb = size / (1024 * 1024)
    await status_msg.edit_text(f"📤 <b>Загружаю большой файл ({size_mb:.1f} МБ) в Telegram...</b> 0%")

    try:
        entity = await mtproto_client.get_input_entity(target_chat_id)
    except Exception:
        if fallback_username:
            entity = await mtproto_client.get_input_entity(fallback_username)
        else:
            raise RuntimeError(
                "Не удалось определить чат для MTProto-отправки. Отправь боту ещё одно сообщение и повтори."
            )

    last_edit = [0.0]

    async def progress_cb(sent: int, total: int) -> None:
        now = time.monotonic()
        if now - last_edit[0] < 2.5 and sent < total:
            return
        last_edit[0] = now
        pct = min(99, int(sent * 100 / max(total, 1)))
        sent_mb = sent / (1024 * 1024)
        tot_mb = total / (1024 * 1024)
        try:
            await status_msg.edit_text(
                f"📤 <b>Загружаю большой файл в Telegram...</b> {pct}% ({sent_mb:.1f} / {tot_mb:.1f} МБ)"
            )
        except Exception:
            pass

    if mode == "mp3":
        attrs = [
            DocumentAttributeAudio(
                duration=duration,
                title=title_raw,
                performer=uploader_raw or "",
            )
        ]
        tg_btn = [[TgButton.inline("🎬 Заменить на видео", f"sw:{token}:best".encode())]]
    else:
        attrs = [
            DocumentAttributeVideo(
                duration=duration,
                w=width or 1280,
                h=height or 720,
                supports_streaming=True,
            )
        ]
        tg_btn = [[TgButton.inline("🎵 Заменить на музыку (MP3)", f"sw:{token}:mp3".encode())]]

    await mtproto_client.send_file(
        entity,
        file=str(file_path),
        caption=caption,
        parse_mode="html",
        supports_streaming=(mode != "mp3"),
        attributes=attrs,
        buttons=tg_btn,
        reply_to=reply_to_message_id,
        progress_callback=progress_cb,
    )


async def present_url_options(message: Message, url: str) -> None:
    token = store_url_entry(url)
    wait_msg = await message.reply("🔎 <b>Анализирую ссылку...</b>")
    loop = asyncio.get_running_loop()
    try:
        meta = await loop.run_in_executor(executor, extract_info_sync, url)
        raw_sizes = meta.get("raw_sizes") or {}
        str_sizes = meta.get("sizes") or {}
        url_store[token]["raw_sizes"] = raw_sizes
        url_store[token]["sizes"] = str_sizes

        title = html.escape(meta["title"][:120])
        uploader = html.escape(meta["uploader"][:60])
        platform = html.escape(meta["extractor"])
        dur = format_duration(meta["duration"])
        details = " · ".join(x for x in [platform, uploader, dur] if x)

        known_sizes = [v for v in raw_sizes.values() if v > 0]
        if known_sizes and all(v > MAX_TG_SIZE_BYTES for v in known_sizes):
            min_mb = min(known_sizes) / (1024 * 1024)
            await wait_msg.edit_text(
                f"🎬 <b>{title}</b>\n"
                f"<i>{details}</i>\n\n"
                f"⚠️ <b>Загрузка отменена:</b> файл слишком большой (минимум ~{min_mb:.1f} МБ), "
                f"а лимит Telegram — {MAX_LIMIT_LABEL}."
            )
            return

        await wait_msg.edit_text(
            f"🎬 <b>{title}</b>\n"
            f"<i>{details}</i>\n\n"
            "Выбери формат для скачивания:",
            reply_markup=build_format_keyboard(token, str_sizes, raw_sizes),
        )
    except Exception as exc:
        logger.warning("extract_info failed for %s: %s", url, exc)
        low = str(exc).lower()
        if "[instagram:user]" in low or "sign in to confirm" in low or "not a bot" in low:
            await wait_msg.edit_text(f"❌ <b>Не удалось загрузить:</b>\n{humanize_error(exc)}")
            return
        await wait_msg.edit_text(
            "🔗 Ссылка принята. Выбери формат для скачивания:",
            reply_markup=build_format_keyboard(token),
        )


async def direct_download_and_send(
    bot: Bot,
    target_chat_id: int,
    status_msg: Message,
    url: str,
    mode: str,
    user_id: int | None,
    reply_to_message_id: int | None = None,
    fallback_username: str | None = None,
) -> bool:
    """Download media and send directly to target_chat_id (auto-picking best quality <= MAX_TG_SIZE_BYTES when mode='auto')."""
    loop = asyncio.get_running_loop()
    raw_sizes: dict = {}
    str_sizes: dict = {}
    chosen_mode = mode

    try:
        meta = await loop.run_in_executor(executor, extract_info_sync, url)
        raw_sizes = meta.get("raw_sizes") or {}
        str_sizes = meta.get("sizes") or {}
    except Exception:
        pass

    token = store_url_entry(url, raw_sizes, str_sizes)

    if mode == "auto":
        chosen_mode = "best"
        for candidate in ("best", "720", "480"):
            sz = raw_sizes.get(candidate, 0)
            if sz == 0 or sz <= MAX_TG_SIZE_BYTES:
                chosen_mode = candidate
                break
        else:
            min_v = min((raw_sizes.get(c, 0) for c in ("best", "720", "480") if raw_sizes.get(c, 0) > 0), default=0)
            if min_v > MAX_TG_SIZE_BYTES:
                await status_msg.edit_text(
                    f"⚠️ <b>Загрузка отменена:</b> видео больше {MAX_LIMIT_LABEL} (~{min_v / (1024*1024):.1f} МБ). "
                    f"Попробуй скачать только звук командой <code>/mp3</code>."
                )
                return False
    else:
        est = raw_sizes.get(chosen_mode, 0)
        if est > MAX_TG_SIZE_BYTES:
            await status_msg.edit_text(
                f"⚠️ <b>Загрузка отменена:</b> выбранный формат весит ~{est / (1024*1024):.1f} МБ "
                f"(лимит Telegram — {MAX_LIMIT_LABEL})."
            )
            return False

    label = "MP3" if chosen_mode == "mp3" else ("Макс." if chosen_mode == "best" else f"{chosen_mode}p")
    await status_msg.edit_text(f"⏳ <b>Скачиваю ({label})...</b>")

    temp_dir = Path(tempfile.mkdtemp(prefix="maxgrab_bot_"))
    try:
        file_path, info = await loop.run_in_executor(executor, download_media_sync, url, chosen_mode, temp_dir)
        size = file_path.stat().st_size
        if size > MAX_TG_SIZE_BYTES:
            await status_msg.edit_text(
                f"⚠️ Файл получился больше {MAX_LIMIT_LABEL} (<b>{size / (1024*1024):.1f} МБ</b>)."
            )
            return False

        title = html.escape((info.get("title") or file_path.stem)[:120])
        uploader = html.escape((info.get("uploader") or "")[:60])
        caption = f"🟢 <b>{title}</b>" + (f"\n<i>{uploader}</i>" if uploader else "") + f"\n\n⚡ @{BOT_USERNAME}"
        caption = caption[:1024]

        await send_media_smart(
            bot=bot,
            target_chat_id=target_chat_id,
            file_path=file_path,
            info=info,
            mode=chosen_mode,
            token=token,
            caption=caption,
            status_msg=status_msg,
            reply_to_message_id=reply_to_message_id,
            fallback_username=fallback_username,
        )

        platform = info.get("extractor_key") or info.get("extractor") or "Web"
        if user_id:
            track_download(user_id, platform, chosen_mode)
        return True
    except Exception as exc:
        logger.exception("Direct download failed for %s", url)
        await status_msg.edit_text(f"❌ <b>Ошибка загрузки:</b>\n{humanize_error(exc)}")
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ================= BOT ADDED / REMOVED FROM GROUP =================
@router.my_chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def on_bot_added_to_chat(event: ChatMemberUpdated) -> None:
    if event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    track_chat(event.chat)
    await event.answer(
        "🟢 <b>MaxGrab подключён к этому чату!</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Теперь здесь можно скачивать видео и музыку с YouTube, TikTok, Instagram, VK, SoundCloud и 1000+ сайтов:\n\n"
        "• <code>/get ссылка</code> — сразу скинуть видео в чат\n"
        "• <code>/dl ссылка</code> — выбрать качество видео или MP3\n"
        "• <code>/mp3 ссылка</code> — сразу скинуть музыку (MP3)\n"
        "• Или просто ответь командой <code>/get</code> / <code>/mp3</code> на чужое сообщение со ссылкой!\n\n"
        "<i>Если выдать боту права админа (или отключить Group Privacy), он будет сам подхватывать ссылки на ролики без команд.</i>"
    )


@router.my_chat_member(ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER))
async def on_bot_removed_from_chat(event: ChatMemberUpdated) -> None:
    remove_chat(event.chat.id)


# ================= COMMANDS =================
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    track_user(message.from_user)
    track_chat(message.chat)
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply(
            "🟢 <b>MaxGrab работает в этом чате!</b>\n\n"
            "• <code>/get ссылка</code> — сразу скачать видео\n"
            "• <code>/dl ссылка</code> — выбрать качество / MP3\n"
            "• <code>/mp3 ссылка</code> — скачать только музыку"
        )
        return

    await message.answer(
        "🟢 <b>MaxGrab</b> — видео и музыка в один клик.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Просто отправь ссылку на ролик или трек — я покажу превью и дам выбрать формат.\n\n"
        "🎬 <b>Видео:</b> MP4 (Макс. / 720p / 480p)\n"
        "🎵 <b>Аудио:</b> MP3 (192 kbps)\n"
        "💬 <b>Группы:</b> добавь меня в любой чат и качай видео командой <code>/get</code> или <code>/dl</code>\n"
        "⚡ <b>Площадки:</b> YouTube, TikTok, Instagram, VK, SoundCloud, Twitch, X и 1000+ сайтов\n\n"
        "<i>Отправь первую ссылку прямо сейчас ↓</i>",
        reply_markup=build_start_keyboard(),
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    track_user(message.from_user)
    track_chat(message.chat)
    await message.answer(get_stats_text())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    track_user(message.from_user)
    track_chat(message.chat)
    await message.answer(
        "💡 <b>Как пользоваться MaxGrab</b>\n\n"
        "<b>В личных сообщениях:</b>\n"
        "Просто отправь ссылку и выбери формат.\n\n"
        "<b>В групповых чатах:</b>\n"
        "• <code>/get ссылка</code> — сразу скачать и отправить видео в чат\n"
        "• <code>/dl ссылка</code> — выбрать качество видео или MP3\n"
        "• <code>/mp3 ссылка</code> — скачать только аудио (MP3)\n"
        "• Можно отвечать этими командами на чужое сообщение со ссылкой!\n\n"
        "<b>Отправка видео в свои чаты из лички:</b>\n"
        "• <code>/chats</code> — список чатов, куда добавлен бот\n"
        "• <code>/send ID_чата ссылка</code> — скачать и скинуть видео прямо в тот чат\n"
        "• Или ответь <code>/send ID_чата</code> на любое видео в личке с ботом\n\n"
        "<i>Лимит размера файла — " + MAX_LIMIT_LABEL + ".</i>"
    )


@router.message(Command("chats"))
async def cmd_chats(message: Message) -> None:
    track_user(message.from_user)
    track_chat(message.chat)
    chats = list_tracked_chats()
    if not chats:
        await message.reply(
            "💬 Бот пока не добавлен ни в один групповой чат.\n\n"
            f"Нажми кнопку <b>«➕ Добавить бота в чат»</b> в /start или добавь @{BOT_USERNAME} в группу вручную."
        )
        return

    lines = [f"• <b>{html.escape(title)}</b> — <code>{cid}</code>" for cid, title in chats]
    await message.reply(
        "💬 <b>Чаты, куда добавлен MaxGrab:</b>\n\n"
        + "\n".join(lines)
        + "\n\nЧтобы скинуть видео в чат:\n"
        "• <code>/send ID_чата ссылка</code>\n"
        "• или ответь <code>/send ID_чата</code> на любое видео/аудио здесь."
    )


@router.message(Command("dl", "video"))
async def cmd_dl(message: Message, command: CommandObject) -> None:
    track_user(message.from_user)
    track_chat(message.chat)
    url = extract_url_from_message(message, command.args)
    if not url:
        await message.reply(
            "Укажи ссылку после команды или ответь на сообщение со ссылкой:\n"
            "<code>/dl https://...</code>"
        )
        return
    await present_url_options(message, url)


@router.message(Command("get"))
async def cmd_get(message: Message, command: CommandObject, bot: Bot) -> None:
    track_user(message.from_user)
    track_chat(message.chat)
    url = extract_url_from_message(message, command.args)
    if not url:
        await message.reply(
            "Укажи ссылку после команды или ответь на сообщение со ссылкой:\n"
            "<code>/get https://...</code>"
        )
        return
    status_msg = await message.reply("🔎 <b>Подготавливаю видео...</b>")
    ok = await direct_download_and_send(
        bot=bot,
        target_chat_id=message.chat.id,
        status_msg=status_msg,
        url=url,
        mode="auto",
        user_id=message.from_user.id if message.from_user else None,
        reply_to_message_id=message.message_id,
    )
    if ok:
        try:
            await status_msg.delete()
        except Exception:
            pass


@router.message(Command("mp3", "music", "audio"))
async def cmd_mp3(message: Message, command: CommandObject, bot: Bot) -> None:
    track_user(message.from_user)
    track_chat(message.chat)
    url = extract_url_from_message(message, command.args)
    if not url:
        await message.reply(
            "Укажи ссылку после команды или ответь на сообщение со ссылкой:\n"
            "<code>/mp3 https://...</code>"
        )
        return
    status_msg = await message.reply("🔎 <b>Подготавливаю аудио (MP3)...</b>")
    ok = await direct_download_and_send(
        bot=bot,
        target_chat_id=message.chat.id,
        status_msg=status_msg,
        url=url,
        mode="mp3",
        user_id=message.from_user.id if message.from_user else None,
        reply_to_message_id=message.message_id,
    )
    if ok:
        try:
            await status_msg.delete()
        except Exception:
            pass


@router.message(Command("send"))
async def cmd_send_to_chat(message: Message, command: CommandObject, bot: Bot) -> None:
    track_user(message.from_user)
    args = (command.args or "").strip()
    parts = args.split(maxsplit=1)
    if not parts or not re.fullmatch(r"-?\d+", parts[0]):
        await message.reply(
            "Использование:\n"
            "• <code>/send ID_чата ссылка</code> — скачать и скинуть видео в группу\n"
            "• Ответь <code>/send ID_чата</code> на готовое видео/аудио — перешлёт его в группу\n\n"
            "Узнать ID добавленных групп: /chats"
        )
        return

    target_chat_id = int(parts[0])
    rest = parts[1] if len(parts) > 1 else ""
    url = extract_url_from_message(message, rest)

    # Case 1: Replying to an existing media message (video/audio/document/photo) -> copy directly into group!
    if message.reply_to_message and not url:
        try:
            await bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=message.chat.id,
                message_id=message.reply_to_message.message_id,
            )
            await message.reply(f"✅ Отправлено в чат <code>{target_chat_id}</code>!")
        except Exception as exc:
            await message.reply(f"❌ Не удалось отправить в чат <code>{target_chat_id}</code>:\n<code>{html.escape(str(exc)[:250])}</code>")
        return

    # Case 2: URL provided -> download and drop directly into target group chat!
    if not url:
        await message.reply("Укажи ссылку после ID чата или ответь этой командой на видео/ссылку.")
        return

    status_msg = await message.reply(f"🔎 <b>Скачиваю видео для чата</b> <code>{target_chat_id}</code>...")
    ok = await direct_download_and_send(
        bot=bot,
        target_chat_id=target_chat_id,
        status_msg=status_msg,
        url=url,
        mode="auto",
        user_id=message.from_user.id if message.from_user else None,
    )
    if ok:
        await status_msg.edit_text(f"✅ <b>Видео отправлено в чат</b> <code>{target_chat_id}</code>!")


# ================= INLINE UI CALLBACKS =================
@router.callback_query(F.data == "ui:sites")
async def ui_sites(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "🌐 <b>Поддерживаемые площадки</b>\n\n"
        "• YouTube & YouTube Shorts\n"
        "• TikTok (без водяного знака)\n"
        "• Instagram Reels & посты\n"
        "• VK Видео & Клипы\n"
        "• SoundCloud\n"
        "• Twitch клипы\n"
        "• X (Twitter)\n"
        "• И ещё более <b>1000 сайтов</b>!"
    )


@router.callback_query(F.data == "ui:help")
async def ui_help(callback: CallbackQuery) -> None:
    await callback.answer()
    await cmd_help(callback.message)


# ================= TEXT / URL HANDLER =================
@router.message(F.text)
async def handle_url(message: Message) -> None:
    track_user(message.from_user)
    track_chat(message.chat)
    text = (message.text or "").strip()
    is_group = message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

    match = URL_REGEX.search(text)
    if not match:
        # Never spam regular conversation in group chats!
        if not is_group:
            await message.reply(
                "🔗 Отправь ссылку на видео или трек (начинается с <code>https://</code>)."
            )
        return

    url = match.group(0)
    # In group chats, only auto-trigger on known social/video platforms
    if is_group and not is_supported_group_url(url):
        return

    await present_url_options(message, url)


@router.callback_query(F.data.startswith("dl:") | F.data.startswith("sw:"))
async def handle_download(callback: CallbackQuery, bot: Bot) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректный запрос", show_alert=True)
        return

    action, token, mode = parts
    entry = url_store.get(token)
    if not entry:
        await callback.answer("Ссылка устарела — отправь её ещё раз.", show_alert=True)
        return

    url = entry["url"]
    est_bytes = (entry.get("raw_sizes") or {}).get(mode, 0)
    if est_bytes > MAX_TG_SIZE_BYTES:
        est_mb = est_bytes / (1024 * 1024)
        await callback.answer(
            f"⚠️ Этот формат весит ~{est_mb:.1f} МБ (лимит — {MAX_LIMIT_LABEL}). Выбери качество ниже или MP3!",
            show_alert=True,
        )
        return

    label = "MP3" if mode == "mp3" else ("Макс." if mode == "best" else f"{mode}p")
    await callback.answer(f"Скачиваю ({label})...")

    if action == "sw":
        try:
            await callback.message.delete()
        except Exception:
            pass
        status_msg = await callback.message.answer(f"⏳ <b>Скачиваю ({label})...</b>")
    else:
        status_msg = await callback.message.edit_text(f"⏳ <b>Скачиваю ({label})...</b>")

    temp_dir = Path(tempfile.mkdtemp(prefix="maxgrab_bot_"))
    loop = asyncio.get_running_loop()
    try:
        file_path, info = await loop.run_in_executor(executor, download_media_sync, url, mode, temp_dir)
        size = file_path.stat().st_size

        if size > MAX_TG_SIZE_BYTES:
            size_mb = size / (1024 * 1024)
            await status_msg.edit_text(
                f"⚠️ Файл получился слишком большим (<b>{size_mb:.1f} МБ</b>), "
                f"лимит — {MAX_LIMIT_LABEL}.\n\n"
                f"Выбери качество пониже (480p) или <b>🎵 Музыка · MP3</b>:",
                reply_markup=build_format_keyboard(token),
            )
            return

        title = html.escape((info.get("title") or file_path.stem)[:120])
        uploader = html.escape((info.get("uploader") or "")[:60])
        caption = f"🟢 <b>{title}</b>" + (f"\n<i>{uploader}</i>" if uploader else "") + f"\n\n⚡ @{BOT_USERNAME}"
        caption = caption[:1024]

        fallback_un = (
            callback.message.chat.username
            if callback.message and callback.message.chat.username
            else (callback.from_user.username if callback.from_user else None)
        )
        await send_media_smart(
            bot=bot,
            target_chat_id=callback.message.chat.id,
            file_path=file_path,
            info=info,
            mode=mode,
            token=token,
            caption=caption,
            status_msg=status_msg,
            fallback_username=fallback_un,
        )

        platform = info.get("extractor_key") or info.get("extractor") or "Web"
        if callback.from_user:
            track_user(callback.from_user)
            track_download(callback.from_user.id, platform, mode)
        if callback.message:
            track_chat(callback.message.chat)

        await status_msg.delete()
    except Exception as exc:
        logger.exception("Download failed for %s", url)
        await status_msg.edit_text(f"❌ <b>Ошибка загрузки:</b>\n{humanize_error(exc)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def setup_bot_profile(bot: Bot) -> None:
    global BOT_USERNAME
    try:
        me = await bot.get_me()
        if me.username:
            BOT_USERNAME = me.username

        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню MaxGrab"),
                BotCommand(command="get", description="Быстро скачать видео по ссылке"),
                BotCommand(command="dl", description="Скачать с выбором качества"),
                BotCommand(command="mp3", description="Скачать только музыку (MP3)"),
                BotCommand(command="chats", description="Мои групповые чаты с ботом"),
                BotCommand(command="send", description="Отправить видео в групповой чат"),
                BotCommand(command="stats", description="Статистика бота"),
                BotCommand(command="help", description="Как пользоваться"),
            ],
            scope=BotCommandScopeDefault(),
        )
        await bot.set_my_commands(
            [
                BotCommand(command="get", description="Сразу скачать видео по ссылке"),
                BotCommand(command="dl", description="Выбрать качество видео или MP3"),
                BotCommand(command="mp3", description="Скачать только музыку (MP3)"),
                BotCommand(command="help", description="Команды MaxGrab в чате"),
            ],
            scope=BotCommandScopeAllGroupChats(),
        )
        await bot.set_my_short_description(
            "⬇ Скачивай видео и музыку с YouTube, TikTok, Instagram, VK, SoundCloud и 1000+ сайтов в личке и групповых чатах."
        )
        await bot.set_my_description(
            "🟢 MaxGrab — быстрый загрузчик видео и музыки.\n\n"
            "🎬 Видео в MP4 (Макс. / 720p / 480p)\n"
            "🎵 Музыка и аудио в MP3 (192 kbps)\n"
            "💬 Работает в личке и в любых групповых чатах (/get, /dl, /mp3)\n"
            "⚡ YouTube · TikTok · Instagram · VK · SoundCloud · Twitch · X\n\n"
            "Просто отправь ссылку в чат!"
        )
        logger.info("Профиль бота @%s (включая меню для групп) настроен!", BOT_USERNAME)
    except Exception as e:
        logger.warning("Не удалось обновить часть профиля бота: %s", e)


async def start_health_server() -> web.AppRunner | None:
    """Start a lightweight HTTP healthcheck server when PORT is set (Render, Koyeb, Hugging Face Spaces)."""
    port_str = os.getenv("PORT", "").strip()
    if not port_str.isdigit():
        return None

    async def handle_health(_request: web.Request) -> web.Response:
        return web.Response(text="MaxGrabBot is running 24/7!")

    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(port_str))
    await site.start()
    logger.info("Healthcheck HTTP server listening on 0.0.0.0:%s", port_str)
    return runner


async def main() -> None:
    global COOKIES_PATH
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Укажи BOT_TOKEN в .env или в переменных окружения (Secrets / Env Vars)")

    init_db()
    COOKIES_PATH = init_cookies_file()
    health_runner = await start_health_server()
    await init_mtproto_uploader()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await setup_bot_profile(bot)
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("MaxGrabBot запущен (лимит файлов: %s)!", MAX_LIMIT_LABEL)
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "my_chat_member"],
        )
    finally:
        if mtproto_client:
            await mtproto_client.disconnect()
        if health_runner:
            await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
