import asyncio
import gc
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
from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeChats,
    BotCommandScopeDefault,
    DocumentAttributeAudio,
    DocumentAttributeVideo,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_USERNAME = "maxgrab_media_bot"
BOT_ID = 0

MAX_TG_SIZE_BYTES = 2000 * 1024 * 1024  # 2 GB native MTProto limit
MAX_LIMIT_LABEL = "2 ГБ"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "maxgrab.db"
SESSION_PATH = str(DATA_DIR / "maxgrab_termux")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MaxGrabTermux")

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

URL_REGEX = re.compile(r"https?://\S+")
executor = ThreadPoolExecutor(max_workers=2)
url_store: Dict[str, dict] = {}


# ================= DATABASE =================
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


def track_user(sender) -> None:
    if not sender or not getattr(sender, "id", None):
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
            (
                sender.id,
                getattr(sender, "username", "") or "",
                getattr(sender, "first_name", "") or "",
                now,
                now,
            ),
        )


def track_chat(chat_id: int, title: str | None, is_group: bool) -> None:
    if not is_group:
        return
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO chats (chat_id, title, chat_type, added_at, last_active)
            VALUES (?, ?, 'group', ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                last_active = excluded.last_active
            """,
            (chat_id, title or "Без названия", now, now),
        )


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


# ================= HELPERS =================
def humanize_error(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube запросил авторизацию для этого видео."
    if "[instagram:user]" in low:
        return (
            "Похоже, это ссылка на <b>профиль Instagram</b>, а не на видео.\n"
            "Отправь ссылку именно на конкретный <b>Reels</b> или <b>видео-пост</b>."
        )
    if "private video" in low or "login required" in low or "members-only" in low:
        return "Это видео приватное или доступно только после входа в аккаунт."
    if "requested format is not available" in low:
        return "Не удалось получить доступный формат видео. Попробуй другое качество или MP3."
    if "video unavailable" in low or "not available" in low:
        return "Видео недоступно, удалено или ограничено по региону."
    if "unsupported url" in low:
        return "Эта ссылка не поддерживается — проверь, что она ведёт прямо на видео или трек."
    return f"<code>{html.escape(msg[:280])}</code>"


def format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h > 0 else f"{m}:{sec:02d}"


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
    return raw, {k: format_size(v) for k, v in raw.items()}


def store_url_entry(url: str, raw_sizes: dict | None = None, sizes: dict | None = None) -> str:
    token = uuid.uuid4().hex[:10]
    url_store[token] = {"url": url, "raw_sizes": raw_sizes or {}, "sizes": sizes or {}}
    if len(url_store) > 500:
        url_store.pop(next(iter(url_store)))
    return token


def build_start_buttons():
    return [
        [Button.url("➕ Добавить бота в чат", f"https://t.me/{BOT_USERNAME}?startgroup=true")],
        [Button.inline("🌐 Площадки", b"ui:sites"), Button.inline("💡 Как пользоваться", b"ui:help")],
    ]


def build_format_buttons(token: str, sizes: dict | None = None, raw_sizes: dict | None = None):
    s = sizes or {}
    r = raw_sizes or {}

    def lbl(base: str, key: str) -> str:
        sz = s.get(key, "")
        over = r.get(key, 0) > MAX_TG_SIZE_BYTES
        prefix = "⚠️ " if over else ""
        return f"{prefix}{base} ({sz})" if sz else f"{prefix}{base}"

    return [
        [
            Button.inline(lbl("🎬 Макс.", "best"), f"dl:{token}:best".encode()),
            Button.inline(lbl("🎬 720p", "720"), f"dl:{token}:720".encode()),
        ],
        [
            Button.inline(lbl("🎬 480p", "480"), f"dl:{token}:480".encode()),
            Button.inline(lbl("🎵 MP3", "mp3"), f"dl:{token}:mp3".encode()),
        ],
    ]


def build_switch_buttons(token: str, current_mode: str):
    if current_mode == "mp3":
        return [[Button.inline("🎬 Заменить на видео", f"sw:{token}:best".encode())]]
    return [[Button.inline("🎵 Заменить на музыку (MP3)", f"sw:{token}:mp3".encode())]]


# ================= YT-DLP =================
def _base_ydl_opts() -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "remote_components": ["ejs:github"],
    }
    node_path = shutil.which("node")
    if node_path:
        opts["js_runtimes"] = {"node": {"path": node_path}}
    cookies_local = BASE_DIR / "cookies.txt"
    if cookies_local.is_file() and cookies_local.stat().st_size > 0:
        opts["cookiefile"] = str(cookies_local)
    return opts


def extract_info_sync(url: str) -> dict:
    opts = {**_base_ydl_opts(), "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
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
    opts: dict = {**_base_ydl_opts(), "outtmpl": outtmpl}

    if mode == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        if mode == "best":
            opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        else:
            opts["format"] = (
                f"bestvideo[height<={mode}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={mode}]+bestaudio/best[height<={mode}]/best"
            )
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [
        p for p in temp_dir.iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl", ".tmp"))
    ]
    if not files:
        raise RuntimeError("Файл не был создан после загрузки")
    return files[0], info or {}


async def extract_url_from_event(event, arg_text: str = "") -> str | None:
    if arg_text:
        m = URL_REGEX.search(arg_text)
        if m:
            return m.group(0)
    if event.raw_text:
        m = URL_REGEX.search(event.raw_text)
        if m:
            return m.group(0)
    reply = await event.get_reply_message()
    if reply and reply.raw_text:
        m = URL_REGEX.search(reply.raw_text)
        if m:
            return m.group(0)
    return None


def is_supported_group_url(url: str) -> bool:
    low = url.lower()
    return any(dom in low for dom in GROUP_MEDIA_DOMAINS)


# ================= SEND / DOWNLOAD FLOWS =================
async def send_downloaded_file(
    client: TelegramClient,
    target_chat,
    file_path: Path,
    info: dict,
    mode: str,
    token: str,
    status_msg,
    reply_to: int | None = None,
) -> None:
    size = file_path.stat().st_size
    size_mb = size / (1024 * 1024)
    title_raw = (info.get("title") or file_path.stem)[:64]
    uploader_raw = info.get("uploader") or ""
    duration = int(info.get("duration") or 0)
    width = int(info.get("width") or 1280)
    height = int(info.get("height") or 720)

    title_esc = html.escape((info.get("title") or file_path.stem)[:120])
    uploader_esc = html.escape(uploader_raw[:60])
    caption = (
        f"🟢 <b>{title_esc}</b>"
        + (f"\n<i>{uploader_esc}</i>" if uploader_esc else "")
        + f"\n\n⚡ @{BOT_USERNAME}"
    )[:1024]

    last_edit = [0.0]

    async def progress_cb(sent: int, total: int) -> None:
        if total < 10 * 1024 * 1024:
            return
        now = time.monotonic()
        if now - last_edit[0] < 2.5 and sent < total:
            return
        last_edit[0] = now
        pct = min(99, int(sent * 100 / max(total, 1)))
        try:
            await status_msg.edit(
                f"📤 <b>Отправляю файл...</b> {pct}% ({sent / (1024*1024):.1f} / {total / (1024*1024):.1f} МБ)",
                parse_mode="html",
            )
        except Exception:
            pass

    if size_mb >= 10:
        await status_msg.edit(f"📤 <b>Отправляю файл ({size_mb:.1f} МБ)...</b> 0%", parse_mode="html")
    else:
        await status_msg.edit("📤 <b>Отправляю файл в чат...</b>", parse_mode="html")

    if mode == "mp3":
        attrs = [DocumentAttributeAudio(duration=duration, title=title_raw, performer=uploader_raw)]
    else:
        attrs = [DocumentAttributeVideo(duration=duration, w=width, h=height, supports_streaming=True)]

    await client.send_file(
        target_chat,
        file=str(file_path),
        caption=caption,
        parse_mode="html",
        supports_streaming=(mode != "mp3"),
        attributes=attrs,
        buttons=build_switch_buttons(token, mode),
        reply_to=reply_to,
        progress_callback=progress_cb,
    )


async def present_url_options(event, url: str) -> None:
    token = store_url_entry(url)
    wait_msg = await event.reply("🔎 <b>Анализирую ссылку...</b>", parse_mode="html")
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
            await wait_msg.edit(
                f"🎬 <b>{title}</b>\n<i>{details}</i>\n\n"
                f"⚠️ <b>Загрузка отменена:</b> файл больше {MAX_LIMIT_LABEL} (минимум ~{min_mb:.1f} МБ).",
                parse_mode="html",
            )
            return

        await wait_msg.edit(
            f"🎬 <b>{title}</b>\n<i>{details}</i>\n\nВыбери формат для скачивания:",
            buttons=build_format_buttons(token, str_sizes, raw_sizes),
            parse_mode="html",
        )
    except Exception as exc:
        logger.warning("extract_info failed for %s: %s", url, exc)
        low = str(exc).lower()
        if "[instagram:user]" in low or "sign in to confirm" in low or "not a bot" in low:
            await wait_msg.edit(f"❌ <b>Не удалось загрузить:</b>\n{humanize_error(exc)}", parse_mode="html")
            return
        await wait_msg.edit(
            "🔗 Ссылка принята. Выбери формат для скачивания:",
            buttons=build_format_buttons(token),
            parse_mode="html",
        )


async def direct_download(
    client: TelegramClient,
    target_chat,
    status_msg,
    url: str,
    mode: str,
    user_id: int | None,
    reply_to: int | None = None,
) -> bool:
    loop = asyncio.get_running_loop()
    raw_sizes: dict = {}
    str_sizes: dict = {}
    chosen = mode

    try:
        meta = await loop.run_in_executor(executor, extract_info_sync, url)
        raw_sizes = meta.get("raw_sizes") or {}
        str_sizes = meta.get("sizes") or {}
    except Exception:
        pass

    token = store_url_entry(url, raw_sizes, str_sizes)
    if mode == "auto":
        chosen = "best"
        for c in ("best", "720", "480"):
            sz = raw_sizes.get(c, 0)
            if sz == 0 or sz <= MAX_TG_SIZE_BYTES:
                chosen = c
                break

    label = "MP3" if chosen == "mp3" else ("Макс." if chosen == "best" else f"{chosen}p")
    await status_msg.edit(f"⏳ <b>Скачиваю ({label})...</b>", parse_mode="html")

    temp_dir = Path(tempfile.mkdtemp(prefix="maxgrab_t_"))
    try:
        file_path, info = await loop.run_in_executor(executor, download_media_sync, url, chosen, temp_dir)
        size = file_path.stat().st_size
        if size > MAX_TG_SIZE_BYTES:
            await status_msg.edit(
                f"⚠️ Файл больше {MAX_LIMIT_LABEL} (<b>{size / (1024*1024):.1f} МБ</b>).",
                parse_mode="html",
            )
            return False

        await send_downloaded_file(client, target_chat, file_path, info, chosen, token, status_msg, reply_to=reply_to)
        platform = info.get("extractor_key") or info.get("extractor") or "Web"
        if user_id:
            track_download(user_id, platform, chosen)
        return True
    except Exception as exc:
        logger.exception("Direct download failed for %s", url)
        await status_msg.edit(f"❌ <b>Ошибка загрузки:</b>\n{humanize_error(exc)}", parse_mode="html")
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        gc.collect()


# ================= MAIN BOT =================
async def main() -> None:
    global BOT_USERNAME, BOT_ID
    if not BOT_TOKEN or not API_ID or not API_HASH:
        raise SystemExit("Укажи BOT_TOKEN, API_ID и API_HASH в файле .env")

    init_db()
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    BOT_ID = me.id
    if me.username:
        BOT_USERNAME = me.username

    try:
        await client(
            SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="",
                commands=[
                    BotCommand("start", "Главное меню MaxGrab"),
                    BotCommand("get", "Быстро скачать видео по ссылке"),
                    BotCommand("dl", "Скачать с выбором качества"),
                    BotCommand("mp3", "Скачать только музыку (MP3)"),
                    BotCommand("chats", "Мои групповые чаты с ботом"),
                    BotCommand("send", "Отправить видео в групповой чат"),
                    BotCommand("stats", "Статистика бота"),
                    BotCommand("help", "Как пользоваться"),
                ],
            )
        )
        await client(
            SetBotCommandsRequest(
                scope=BotCommandScopeChats(),
                lang_code="",
                commands=[
                    BotCommand("get", "Сразу скачать видео по ссылке"),
                    BotCommand("dl", "Выбрать качество видео или MP3"),
                    BotCommand("mp3", "Скачать только музыку (MP3)"),
                    BotCommand("help", "Команды MaxGrab в чате"),
                ],
            )
        )
    except Exception as exc:
        logger.warning("Не удалось обновить команды меню: %s", exc)

    async def remember(event):
        sender = await event.get_sender()
        chat = await event.get_chat()
        track_user(sender)
        track_chat(event.chat_id, getattr(chat, "title", None), event.is_group)
        return sender

    @client.on(events.ChatAction)
    async def on_chat_action(event):
        if event.is_group and (event.user_added or event.created):
            if BOT_ID in (event.user_ids or []):
                chat = await event.get_chat()
                track_chat(event.chat_id, getattr(chat, "title", None), True)
                await event.reply(
                    "🟢 <b>MaxGrab подключён к этому чату!</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "• <code>/get ссылка</code> — сразу скинуть видео в чат\n"
                    "• <code>/dl ссылка</code> — выбрать качество видео или MP3\n"
                    "• <code>/mp3 ссылка</code> — сразу скинуть музыку (MP3)\n"
                    "• Или ответь командой <code>/get</code> / <code>/mp3</code> на сообщение со ссылкой!",
                    parse_mode="html",
                )

    @client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?(?:\s|$)"))
    async def on_start(event):
        await remember(event)
        if event.is_group:
            await event.reply(
                "🟢 <b>MaxGrab работает в этом чате!</b>\n\n"
                "• <code>/get ссылка</code> — сразу скачать видео\n"
                "• <code>/dl ссылка</code> — выбрать качество / MP3\n"
                "• <code>/mp3 ссылка</code> — скачать только музыку",
                parse_mode="html",
            )
            return
        await event.respond(
            "🟢 <b>MaxGrab</b> — видео и музыка в один клик.\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Просто отправь ссылку на ролик или трек — я покажу превью и дам выбрать формат.\n\n"
            f"🎬 <b>Видео:</b> MP4 (до {MAX_LIMIT_LABEL})\n"
            "🎵 <b>Аудио:</b> MP3 (192 kbps)\n"
            "💬 <b>Группы:</b> добавь меня в любой чат и качай видео командой <code>/get</code> или <code>/dl</code>\n"
            "⚡ <b>Площадки:</b> YouTube, TikTok, Instagram, VK, SoundCloud, Twitch, X и 1000+ сайтов\n\n"
            "<i>Отправь первую ссылку прямо сейчас ↓</i>",
            buttons=build_start_buttons(),
            parse_mode="html",
        )

    @client.on(events.NewMessage(pattern=r"^/stats(?:@\w+)?(?:\s|$)"))
    async def on_stats(event):
        await remember(event)
        await event.reply(get_stats_text(), parse_mode="html")

    @client.on(events.NewMessage(pattern=r"^/help(?:@\w+)?(?:\s|$)"))
    async def on_help(event):
        await remember(event)
        await event.reply(
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
            "• <code>/send ID_чата ссылка</code> — скачать и скинуть видео прямо в тот чат\n\n"
            f"<i>Лимит размера файла — {MAX_LIMIT_LABEL}.</i>",
            parse_mode="html",
        )

    @client.on(events.NewMessage(pattern=r"^/chats(?:@\w+)?(?:\s|$)"))
    async def on_chats(event):
        await remember(event)
        chats = list_tracked_chats()
        if not chats:
            await event.reply(
                "💬 Бот пока не добавлен ни в один групповой чат.\n\n"
                f"Нажми кнопку <b>«➕ Добавить бота в чат»</b> в /start или добавь @{BOT_USERNAME} вручную.",
                parse_mode="html",
            )
            return
        lines = [f"• <b>{html.escape(title)}</b> — <code>{cid}</code>" for cid, title in chats]
        await event.reply(
            "💬 <b>Чаты, куда добавлен MaxGrab:</b>\n\n"
            + "\n".join(lines)
            + "\n\nЧтобы скинуть видео в чат:\n"
            "• <code>/send ID_чата ссылка</code>\n"
            "• или ответь <code>/send ID_чата</code> на готовое видео/аудио.",
            parse_mode="html",
        )

    @client.on(events.NewMessage(pattern=r"^/(?:dl|video)(?:@\w+)?(?:\s+(.*))?$"))
    async def on_dl(event):
        await remember(event)
        arg = event.pattern_match.group(1) or ""
        url = await extract_url_from_event(event, arg)
        if not url:
            await event.reply("Укажи ссылку после команды или ответь на сообщение со ссылкой:\n<code>/dl https://...</code>", parse_mode="html")
            return
        await present_url_options(event, url)

    @client.on(events.NewMessage(pattern=r"^/get(?:@\w+)?(?:\s+(.*))?$"))
    async def on_get(event):
        sender = await remember(event)
        arg = event.pattern_match.group(1) or ""
        url = await extract_url_from_event(event, arg)
        if not url:
            await event.reply("Укажи ссылку после команды или ответь на сообщение со ссылкой:\n<code>/get https://...</code>", parse_mode="html")
            return
        st = await event.reply("🔎 <b>Подготавливаю видео...</b>", parse_mode="html")
        ok = await direct_download(client, event.chat_id, st, url, "auto", sender.id if sender else None, reply_to=event.id)
        if ok:
            try:
                await st.delete()
            except Exception:
                pass

    @client.on(events.NewMessage(pattern=r"^/(?:mp3|music|audio)(?:@\w+)?(?:\s+(.*))?$"))
    async def on_mp3(event):
        sender = await remember(event)
        arg = event.pattern_match.group(1) or ""
        url = await extract_url_from_event(event, arg)
        if not url:
            await event.reply("Укажи ссылку после команды или ответь на сообщение со ссылкой:\n<code>/mp3 https://...</code>", parse_mode="html")
            return
        st = await event.reply("🔎 <b>Подготавливаю аудио (MP3)...</b>", parse_mode="html")
        ok = await direct_download(client, event.chat_id, st, url, "mp3", sender.id if sender else None, reply_to=event.id)
        if ok:
            try:
                await st.delete()
            except Exception:
                pass

    @client.on(events.NewMessage(pattern=r"^/send(?:@\w+)?(?:\s+(.*))?$"))
    async def on_send(event):
        sender = await remember(event)
        args = (event.pattern_match.group(1) or "").strip()
        parts = args.split(maxsplit=1)
        if not parts or not re.fullmatch(r"-?\d+", parts[0]):
            await event.reply(
                "Использование:\n"
                "• <code>/send ID_чата ссылка</code> — скачать и скинуть видео в группу\n"
                "• Ответь <code>/send ID_чата</code> на видео/аудио — отправит его в группу\n\n"
                "Список групп: /chats",
                parse_mode="html",
            )
            return

        target_id = int(parts[0])
        rest = parts[1] if len(parts) > 1 else ""
        url = await extract_url_from_event(event, rest)
        reply = await event.get_reply_message()

        if reply and not url:
            try:
                await client.send_message(target_id, reply)
                await event.reply(f"✅ Отправлено в чат <code>{target_id}</code>!", parse_mode="html")
            except Exception as exc:
                await event.reply(f"❌ Не удалось отправить:\n<code>{html.escape(str(exc)[:250])}</code>", parse_mode="html")
            return

        if not url:
            await event.reply("Укажи ссылку после ID чата или ответь этой командой на видео.")
            return

        st = await event.reply(f"🔎 <b>Скачиваю видео для чата</b> <code>{target_id}</code>...", parse_mode="html")
        ok = await direct_download(client, target_id, st, url, "auto", sender.id if sender else None)
        if ok:
            await st.edit(f"✅ <b>Видео отправлено в чат</b> <code>{target_id}</code>!", parse_mode="html")

    @client.on(events.NewMessage)
    async def on_plain_message(event):
        text = (event.raw_text or "").strip()
        if not text or text.startswith("/"):
            return
        await remember(event)
        m = URL_REGEX.search(text)
        if not m:
            if event.is_private:
                await event.reply("🔗 Отправь ссылку на видео или трек (начинается с <code>https://</code>).", parse_mode="html")
            return
        url = m.group(0)
        if event.is_group and not is_supported_group_url(url):
            return
        await present_url_options(event, url)

    @client.on(events.CallbackQuery)
    async def on_callback(event):
        data = (event.data or b"").decode("utf-8", errors="ignore")
        if data == "ui:sites":
            await event.answer()
            await event.respond(
                "🌐 <b>Поддерживаемые площадки</b>\n\n"
                "• YouTube & YouTube Shorts\n"
                "• TikTok (без водяного знака)\n"
                "• Instagram Reels & посты\n"
                "• VK Видео & Клипы\n"
                "• SoundCloud · Twitch · X (Twitter)\n"
                "• И ещё более <b>1000 сайтов</b>!",
                parse_mode="html",
            )
            return
        if data == "ui:help":
            await event.answer()
            await on_help(event)
            return

        if not (data.startswith("dl:") or data.startswith("sw:")):
            await event.answer()
            return

        parts = data.split(":")
        if len(parts) != 3:
            await event.answer("Некорректный запрос", alert=True)
            return

        action, token, mode = parts
        entry = url_store.get(token)
        if not entry:
            await event.answer("Ссылка устарела — отправь её ещё раз.", alert=True)
            return

        url = entry["url"]
        est_bytes = (entry.get("raw_sizes") or {}).get(mode, 0)
        if est_bytes > MAX_TG_SIZE_BYTES:
            await event.answer(f"⚠️ Этот формат больше {MAX_LIMIT_LABEL}. Выбери качество ниже или MP3!", alert=True)
            return

        label = "MP3" if mode == "mp3" else ("Макс." if mode == "best" else f"{mode}p")
        await event.answer(f"Скачиваю ({label})...")

        if action == "sw":
            try:
                await event.delete()
            except Exception:
                pass
            status_msg = await client.send_message(event.chat_id, f"⏳ <b>Скачиваю ({label})...</b>", parse_mode="html")
        else:
            status_msg = await event.edit(f"⏳ <b>Скачиваю ({label})...</b>", parse_mode="html")

        temp_dir = Path(tempfile.mkdtemp(prefix="maxgrab_t_"))
        loop = asyncio.get_running_loop()
        try:
            file_path, info = await loop.run_in_executor(executor, download_media_sync, url, mode, temp_dir)
            size = file_path.stat().st_size
            if size > MAX_TG_SIZE_BYTES:
                await status_msg.edit(
                    f"⚠️ Файл получился больше {MAX_LIMIT_LABEL} (<b>{size / (1024*1024):.1f} МБ</b>).",
                    buttons=build_format_buttons(token),
                    parse_mode="html",
                )
                return

            await send_downloaded_file(client, event.chat_id, file_path, info, mode, token, status_msg)
            sender = await event.get_sender()
            chat = await event.get_chat()
            track_user(sender)
            track_chat(event.chat_id, getattr(chat, "title", None), event.is_group)
            platform = info.get("extractor_key") or info.get("extractor") or "Web"
            if sender:
                track_download(sender.id, platform, mode)
            try:
                await status_msg.delete()
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Download failed for %s", url)
            await status_msg.edit(f"❌ <b>Ошибка загрузки:</b>\n{humanize_error(exc)}", parse_mode="html")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
            gc.collect()

    logger.info("MaxGrabTermux (@%s) запущен на телефоне 24/7! Лимит файлов: %s", BOT_USERNAME, MAX_LIMIT_LABEL)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
