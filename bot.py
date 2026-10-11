# YouTube + Instagram + Facebook Telegram bot
import os
import re
import time
import asyncio
import logging
import tempfile
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

import yt_dlp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------- Настройки ----------
TOKEN = os.getenv("BOT_TOKEN")
MAX_SIZE_MB = int(os.getenv("MAX_SIZE_MB", "48"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
PROXY_URL = os.getenv("PROXY_URL", "").strip()

CHANNEL = "@edrikevideo"
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdl_bot"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ytdl-bot")

# Secret Files Render только для чтения: копируем cookies в /tmp.
LOCAL_COOKIES = Path(__file__).parent / "cookies.txt"
SECRET_COOKIES = Path("/etc/secrets/cookies.txt")
TEMP_COOKIES = Path(tempfile.gettempdir()) / "ytdl_bot_cookies.txt"

COOKIES_FILE = None
if SECRET_COOKIES.exists():
    try:
        shutil.copyfile(SECRET_COOKIES, TEMP_COOKIES)
        COOKIES_FILE = TEMP_COOKIES
    except OSError:
        log.exception("Не удалось скопировать Render Secret File с cookies")
elif LOCAL_COOKIES.exists():
    COOKIES_FILE = LOCAL_COOKIES

log.info("Cookies доступны: %s", bool(COOKIES_FILE and COOKIES_FILE.exists()))
log.info("Прокси настроен: %s", bool(PROXY_URL))

# ---------- Ссылки и платформы ----------
PLATFORM_PATTERNS = [
    ("youtube", re.compile(
        r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/[\w\-?=&/%.]+",
        re.IGNORECASE,
    )),
    ("instagram", re.compile(
        r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv|share)/[\w\-?=&/%.]+",
        re.IGNORECASE,
    )),
    ("facebook", re.compile(
        r"https?://(?:(?:www|m|web|ru-ru)\.)?(?:facebook\.com|fb\.watch|fb\.com)/[\w\-?=&/%.]+",
        re.IGNORECASE,
    )),
]

PLATFORM_NAMES = {
    "youtube": "▶️ YouTube",
    "instagram": "📸 Instagram",
    "facebook": "📘 Facebook",
}


def detect_platform(text: str):
    for platform, pattern in PLATFORM_PATTERNS:
        match = pattern.search(text)
        if match:
            return platform, match.group(0).rstrip(".,)")
    return None, None


def is_youtube_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return (
        host == "youtu.be"
        or host == "youtube.com"
        or host.endswith(".youtube.com")
    )


# ---------- Подписка ----------
async def check_subscription(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        log.exception("Не удалось проверить подписку на %s", CHANNEL)
        return False


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url="https://t.me/edrikevideo")],
        [InlineKeyboardButton("✅ Я подписался", callback_data="sub:check")],
    ])


async def require_subscription(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if await check_subscription(ctx.bot, update.effective_user.id):
        return True

    await update.effective_message.reply_text(
        "Чтобы пользоваться ботом, подпишитесь на канал "
        "@edrikevideo, затем нажмите «✅ Я подписался».",
        reply_markup=subscription_keyboard(),
    )
    return False


# ---------- Меню ----------
REPLY_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🎬 Скачать видео")],
        [KeyboardButton("🎵 Скачать MP3"), KeyboardButton("ℹ️ Помощь")],
        [KeyboardButton("📊 Статус"), KeyboardButton("❌ Отмена")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Отправьте ссылку на видео",
)

YOUTUBE_MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🎬 1080p", callback_data="q:1080"),
        InlineKeyboardButton("🎬 720p", callback_data="q:720"),
    ],
    [
        InlineKeyboardButton("🎬 480p", callback_data="q:480"),
        InlineKeyboardButton("🎬 360p", callback_data="q:360"),
    ],
    [
        InlineKeyboardButton("⭐ Лучшее", callback_data="q:best"),
        InlineKeyboardButton("🎵 MP3", callback_data="q:audio"),
    ],
    [
        InlineKeyboardButton("⬅️ Назад", callback_data="q:back"),
        InlineKeyboardButton("❌ Отмена", callback_data="q:cancel"),
    ],
])

SOCIAL_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Скачать видео", callback_data="q:best")],
    [InlineKeyboardButton("🎵 MP3", callback_data="q:audio")],
    [
        InlineKeyboardButton("⬅️ Назад", callback_data="q:back"),
        InlineKeyboardButton("❌ Отмена", callback_data="q:cancel"),
    ],
])

QUALITY_LABELS = {
    "1080": "1080p",
    "720": "720p",
    "480": "480p",
    "360": "360p",
    "best": "лучшее качество",
    "audio": "MP3",
}

YOUTUBE_FORMATS = {
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720": "bv*[height<=720]+ba/b[height<=720]",
    "480": "bv*[height<=480]+ba/b[height<=480]",
    "360": "bv*[height<=360]+ba/b[height<=360]",
    "best": "bv*+ba/b",
    "audio": "bestaudio/best",
}

YOUTUBE_FALLBACKS = {
    "1080": ["720", "480", "360", "audio"],
    "720": ["480", "360", "audio"],
    "480": ["360", "audio"],
    "360": ["audio"],
    "best": ["720", "480", "360", "audio"],
    "audio": [],
}

# ---------- yt-dlp ----------
def ydl_options(url: str, kind: str, output_template: str) -> dict:
    opts = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 60,
    }

    if COOKIES_FILE and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)

    # Прокси используется только для YouTube.
    if PROXY_URL and is_youtube_url(url):
        opts["proxy"] = PROXY_URL

    if kind == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif is_youtube_url(url):
        opts["format"] = YOUTUBE_FORMATS.get(kind, YOUTUBE_FORMATS["best"])
        opts["merge_output_format"] = "mp4"
    else:
        # Instagram/Facebook обычно предоставляют готовую видеодорожку.
        opts["format"] = "best[ext=mp4]/best"
        opts["merge_output_format"] = "mp4"

    return opts


def extract_info_sync(url: str) -> dict:
    opts = {
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 45,
    }
    if COOKIES_FILE and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if PROXY_URL and is_youtube_url(url):
        opts["proxy"] = PROXY_URL

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_sync(url: str, kind: str) -> Path:
    job_dir = DOWNLOAD_DIR / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)
    template = str(job_dir / "%(title).100B [%(id)s].%(ext)s")

    with yt_dlp.YoutubeDL(ydl_options(url, kind, template)) as ydl:
        info = ydl.extract_info(url, download=True)
        expected = Path(ydl.prepare_filename(info))

    if kind == "audio":
        mp3_files = list(job_dir.glob("*.mp3"))
        if mp3_files:
            return mp3_files[0]

    if expected.exists():
        return expected

    candidates = [
        p for p in job_dir.iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_size)

    raise FileNotFoundError("После скачивания файл не найден")


def cleanup_file(path: Path | None):
    if path is None:
        return
    try:
        folder = path.parent
        for item in folder.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)
        folder.rmdir()
    except OSError:
        log.warning("Не удалось очистить временную папку %s", path.parent)


# ---------- Форматирование и состояние ----------
def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def format_duration(seconds) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(re
