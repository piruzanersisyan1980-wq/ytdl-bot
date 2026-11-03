# bot.py — скачивание видео и аудио из популярных сервисов
import os
import re
import io
import time
import json
import html
import uuid
import asyncio
import logging
import tempfile
import shutil
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, quote
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone, timedelta

import yt_dlp
import qrcode
from gtts import gTTS
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    InlineQueryHandler,
    CallbackQueryHandler,
    filters,
)

# ============================================================
# Настройки
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

MAX_SIZE_MB = int(os.getenv("MAX_SIZE_MB", "48"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))

PROGRESS_EVERY = float(os.getenv("PROGRESS_EVERY", "3"))
FIX_ASPECT = os.getenv("FIX_ASPECT", "0") == "1"

REF_GOAL = int(os.getenv("REF_GOAL", "3"))
WATERMARK = os.getenv("WATERMARK", "@edrikevideo")
FONT_PATH = os.getenv(
    "FONT_PATH",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

BOOKMARKS_MAX = 10
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "20"))

IO_WORKERS = int(os.getenv("IO_WORKERS", "8"))
YTDL_WORKERS = int(os.getenv("YTDL_WORKERS", "4"))
SUB_CACHE_TTL = int(os.getenv("SUB_CACHE_TTL", "300"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "1800"))
DROP_PENDING = os.getenv("DROP_PENDING", "0") == "1"

# Прокси используется только для YouTube.
PROXY_URL = os.getenv("PROXY_URL", "").strip()
PROXY_LIST = [
    item.strip()
    for item in os.getenv("PROXY_LIST", "").split(",")
    if item.strip()
]
PROXY_FALLBACK_DIRECT = os.getenv("PROXY_FALLBACK_DIRECT", "1") == "1"
PROXY_PENALTY_MAX = int(os.getenv("PROXY_PENALTY_MAX", "600"))

if not PROXY_URL and PROXY_LIST:
    PROXY_URL = PROXY_LIST[0]

# polling — для Background Worker и обычного запуска.
# webhook — только для Web Service с публичным HTTPS-адресом.
RUN_MODE = os.getenv("RUN_MODE", "polling").lower().strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram")

CHANNEL = "@edrikevideo"
CHANNEL_URL = "https://t.me/edrikevideo"
CONTACT_TEXT = "напиши @edrikevideo"

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdl_bot"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

REF_FILE = Path(tempfile.gettempdir()) / "ytdl_refs.json"
BK_FILE = Path(tempfile.gettempdir()) / "ytdl_bookmarks.json"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger("ytdl-bot")

# ============================================================
# Пулы потоков
# ============================================================

IO_POOL = ThreadPoolExecutor(
    max_workers=IO_WORKERS,
    thread_name_prefix="bot-io",
)
YTDL_SEM = asyncio.Semaphore(YTDL_WORKERS)


async def run_io(func, *args):
    """Выполнить блокирующую функцию в отдельном потоке."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(IO_POOL, func, *args)


async def run_ytdlp(func, *args):
    """Ограничить количество одновременных yt-dlp операций."""
    async with YTDL_SEM:
        return await run_io(func, *args)


# ============================================================
# Прокси
# ============================================================

_proxy_state = {}


def mask_proxy(proxy):
    if not proxy:
        return ""
    return re.sub(r"://[^@/]+@", "://***:***@", proxy)


def proxy_is_available(proxy):
    blocked_until, _ = _proxy_state.get(proxy, (0, 0))
    return time.time() >= blocked_until


def mark_proxy_failed(proxy):
    if not proxy:
        return

    _, failures = _proxy_state.get(proxy, (0, 0))
    failures += 1
    penalty = min(PROXY_PENALTY_MAX, 15 * failures)

    _proxy_state[proxy] = (time.time() + penalty, failures)

    log.warning(
        "Прокси %s временно отключён на %s сек.",
        mask_proxy(proxy),
        penalty,
    )


def mark_proxy_success(proxy):
    if proxy:
        _proxy_state.pop(proxy, None)


def is_proxy_error(error):
    text = str(error).lower()

    return (
        "unable to connect to proxy" in text
        or "proxyerror" in text
        or "proxy error" in text
        or "unexpected_eof_while_reading" in text
        or "wrong version number" in text
        or ("timed out" in text and "proxy" in text)
    )


def proxy_candidates(platform):
    if platform != "youtube":
        return [None]

    pool = []

    for proxy in ([PROXY_URL] if PROXY_URL else []) + PROXY_LIST:
        if proxy and proxy not in pool:
            pool.append(proxy)

    available = [
        proxy for proxy in pool
        if proxy_is_available(proxy)
    ]

    if PROXY_FALLBACK_DIRECT:
        available.append(None)

    return available or [None]


# ============================================================
# Cookies
# ============================================================

LOCAL_COOKIES = Path(__file__).parent / "cookies.txt"
SECRET_COOKIES = Path("/etc/secrets/cookies.txt")
TEMP_COOKIES = Path(tempfile.gettempdir()) / "ytdl_bot_cookies.txt"

COOKIES_FILE = None

if SECRET_COOKIES.exists():
    try:
        shutil.copyfile(SECRET_COOKIES, TEMP_COOKIES)
        COOKIES_FILE = TEMP_COOKIES
    except OSError:
        log.exception("Не удалось скопировать cookies")
elif LOCAL_COOKIES.exists():
    COOKIES_FILE = LOCAL_COOKIES

log.info(
    "Cookies: %s | Прокси: %s | Режим: %s",
    bool(COOKIES_FILE and COOKIES_FILE.exists()),
    mask_proxy(PROXY_URL) or "выкл",
    RUN_MODE,
)

# ============================================================
# Реферальная система
# ============================================================

_refs = {}
_ref_lock = asyncio.Lock()


def load_refs():
    global _refs

    try:
        if REF_FILE.exists():
            _refs = json.loads(
                REF_FILE.read_text(encoding="utf-8")
            )
    except Exception:
        _refs = {}


def save_refs():
    try:
        REF_FILE.write_text(
            json.dumps(_refs, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.warning("Не удалось сохранить рефералы", exc_info=True)


def ref_link(bot_username, user_id):
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def ref_stats(user_id):
    data = _refs.get(str(user_id), {})
    return data.get("invited", 0), data.get("bonus", 0)


# ============================================================
# Закладки
# ============================================================

_bookmarks = {}
_bookmarks_lock = asyncio.Lock()


def load_bookmarks():
    global _bookmarks

    try:
        if BK_FILE.exists():
            _bookmarks = json.loads(
                BK_FILE.read_text(encoding="utf-8")
            )
    except Exception:
        _bookmarks = {}


def save_bookmarks():
    try:
        BK_FILE.write_text(
            json.dumps(_bookmarks, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.warning("Не удалось сохранить закладки", exc_info=True)


async def add_bookmark(user_id, url, title, platform):
    async with _bookmarks_lock:
        items = _bookmarks.setdefault(str(user_id), [])
        items = [
            item for item in items
            if item.get("url") != url
        ]

        items.insert(0, {
            "url": url,
            "title": (title or "Видео")[:80],
            "platform": platform,
            "ts": int(time.time()),
        })

        _bookmarks[str(user_id)] = items[:BOOKMARKS_MAX]
        await asyncio.to_thread(save_bookmarks)


def get_bookmarks(user_id):
    return _bookmarks.get(str(user_id), [])


# ============================================================
# Проверка подписки
# ============================================================

_subscription_cache = {}


def prune_subscription_cache():
    if len(_subscription_cache) <= 5000:
        return

    oldest = sorted(
        _subscription_cache.items(),
        key=lambda item: item[1][0],
    )[:1000]

    for user_id, _ in oldest:
        _subscription_cache.pop(user_id, None)


async def check_subscription(bot, user_id, force=False):
    now = time.time()

    if not force:
        cached = _subscription_cache.get(user_id)

        if cached and now - cached[0] < SUB_CACHE_TTL:
            return cached[1]

    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        subscribed = member.status not in ("left", "kicked")
    except Exception:
        log.exception("Ошибка проверки подписки")
        subscribed = False

    _subscription_cache[user_id] = (now, subscribed)
    prune_subscription_cache()

    return subscribed


def subscription_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 Подписаться на канал",
                url=CHANNEL_URL,
            )
        ],
        [
            InlineKeyboardButton(
                "✅ Я подписался",
                callback_data="sub:check",
            )
        ],
    ])


async def require_subscription(update, context):
    user = update.effective_user

    if not user:
        return False

    stat_user(user.id, user.username or "")

    if await check_subscription(context.bot, user.id):
        return True

    message = update.effective_message

    if message:
        await message.reply_text(
            f"Чтобы пользоваться ботом, подпишись на {CHANNEL} "
            "и нажми «✅ Я подписался».",
            reply_markup=subscription_keyboard(),
        )

    return False


# ============================================================
# Статистика
# ============================================================

_stats = {
    "users": {},
    "downloads": {},
    "platforms": {},
    "errors": 0,
}


def stat_user(user_id, username=""):
    user = _stats["users"].setdefault(str(user_id), {
        "first_seen": int(time.time()),
        "username": username,
    })

    if username:
        user["username"] = username


def stat_download(platform):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    _stats["downloads"][day] = (
        _stats["downloads"].get(day, 0) + 1
    )
    _stats["platforms"][platform] = (
        _stats["platforms"].get(platform, 0) + 1
    )


# ============================================================
# Платформы
# ============================================================

PLATFORM_PATTERNS = [
    (
        "youtube",
        re.compile(
            r"https?://(?:www\.|m\.)?"
            r"(?:youtube\.com|youtu\.be)/[\w\-?=&/%.]+",
            re.I,
        ),
    ),
    (
        "instagram",
        re.compile(
            r"https?://(?:www\.)?instagram\.com/"
            r"(?:p|reel|reels|tv|share)/[\w\-?=&/%.]+",
            re.I,
        ),
    ),
    (
        "facebook",
        re.compile(
            r"https?://(?:(?:www|m|web|ru-ru)\.)?"
            r"(?:facebook\.com|fb\.watch|fb\.com)/[\w\-?=&/%.]+",
            re.I,
        ),
    ),
    (
        "vk",
        re.compile(
            r"https?://(?:(?:m|new)\.)?"
            r"(?:vk\.com|vkvideo\.ru|vk\.ru)/[\w\-?=&/%.]+",
            re.I,
        ),
    ),
    (
        "tiktok",
        re.compile(
            r"https?://(?:www\.|m\.)?"
            r"(?:tiktok\.com|vt\.tiktok\.com)/[\w\-?=&/%.@]+",
            re.I,
        ),
    ),
    (
        "twitter",
        re.compile(
            r"https?://(?:(?:www\.|mobile\.)?"
            r"(?:twitter\.com|x\.com))/[\w\-?=&/%.]+",
            re.I,
        ),
    ),
    (
        "rutube",
        re.compile(
            r"https?://(?:www\.)?rutube\.ru/"
            r"(?:video|shorts)/[\w\-?=&/%.]+",
            re.I,
        ),
    ),
    (
        "soundcloud",
        re.compile(
            r"https?://(?:www\.|m\.)?"
            r"soundcloud\.com/[\w\-?=&/%.]+",
            re.I,
        ),
    ),
]

PLATFORM_NAMES = {
    "youtube": "▶️ YouTube",
    "instagram": "📸 Instagram",
    "facebook": "📘 Facebook",
    "vk": "🅥 VK",
    "tiktok": "🎵 TikTok",
    "twitter": "🐦 Twitter / X",
    "rutube": "📺 Rutube",
    "soundcloud": "🎧 SoundCloud",
}


def detect_platform(text):
    for platform, pattern in PLATFORM_PATTERNS:
        match = pattern.search(text or "")

        if match:
            return platform, match.group(0).rstrip(".,)")

    return None, None


def platform_from_url(url):
    host = (urlsplit(url).hostname or "").lower()

    if host in ("youtu.be", "youtube.com"):
        return "youtube"
    if host.endswith(".youtube.com"):
        return "youtube"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram"
    if host == "fb.watch" or host.endswith(".fb.watch"):
        return "facebook"
    if host.endswith("facebook.com"):
        return "facebook"
    if (
        host.endswith("vk.com")
        or host.endswith("vk.ru")
        or host.endswith("vkvideo.ru")
    ):
        return "vk"
    if host.endswith("tiktok.com"):
        return "tiktok"
    if host.endswith("twitter.com") or host.endswith("x.com"):
        return "twitter"
    if host.endswith("rutube.ru"):
        return "rutube"
    if host.endswith("soundcloud.com"):
        return "soundcloud"

    return "other"


def unavailable_message():
    return (
        "🛑 Ошибка скачивания!\n"
        "Этот видеоролик недоступен.\n"
        "Попробуй повторить запрос.\n"
        f"Если видео не загрузилось, {CONTACT_TEXT}."
    )


# ============================================================
# Клавиатуры
# ============================================================

REPLY_MENU = ReplyKeyboardMarkup(
    [
        [
            KeyboardButton("🎬 Скачать видео"),
            KeyboardButton("🔍 Поиск"),
        ],
        [
            KeyboardButton("🎵 MP3"),
            KeyboardButton("🛠 Инструменты"),
        ],
        [
            KeyboardButton("⭐ Закладки"),
            KeyboardButton("📊 Статус"),
        ],
        [
            KeyboardButton("🔗 Моя ссылка"),
            KeyboardButton("❌ Отмена"),
        ],
    ],
    resize_keyboard=True,
    input_field_placeholder="Ссылка или поисковый запрос",
)

TOOLS_MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🔳 QR-код", callback_data="tool:qr"),
        InlineKeyboardButton("🎙 Текст в голос", callback_data="tool:tts"),
    ],
    [
        InlineKeyboardButton("🔄 Конвертер", callback_data="tool:conv"),
        InlineKeyboardButton("💧 Водяной знак", callback_data="tool:wm"),
    ],
    [
        InlineKeyboardButton("🔗 Моя ссылка", callback_data="tool:ref"),
    ],
])

CONVERT_MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🎵 MP3", callback_data="conv:mp3"),
        InlineKeyboardButton("🎼 WAV", callback_data="conv:wav"),
    ],
    [
        InlineKeyboardButton("🎬 MP4", callback_data="conv:mp4"),
        InlineKeyboardButton("🍎 M4A", callback_data="conv:m4a"),
    ],
    [
        InlineKeyboardButton("❌ Отмена", callback_data="conv:cancel"),
    ],
])


def share_keyboard(url, bot_username):
    text = quote(
        f"🎬 Скачиваю видео в @{bot_username}! → {url}"
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📤 Поделиться с другом",
                url=(
                    "https://t.me/share/url?"
                    f"url=https://t.me/{bot_username}&text={text}"
                ),
            )
        ],
        [
            InlineKeyboardButton(
                "🎬 Скачать ещё",
                callback_data="share:more",
            )
        ],
    ])


# ============================================================
# Динамические форматы
# ============================================================

LABELS = {
    "1080": "1080p",
    "720": "720p",
    "480": "480p",
    "360": "360p",
    "best": "лучшее качество",
    "audio": "MP3",
}

YT_FALLBACKS = {
    "1080": ["720", "480", "360", "audio"],
    "720": ["480", "360", "audio"],
    "480": ["360", "audio"],
    "360": ["audio"],
    "best": ["720", "480", "360", "audio"],
    "audio": [],
}

SOCIAL_ORDER = ["1080", "720", "480", "360", "best", "audio"]

STANDARD_HEIGHTS = [
    144, 240, 360, 480, 720, 1080, 1440, 2160,
]
MAX_FORMAT_BUTTONS = 6


def format_size(size):
    if not size:
        return "?"

    megabytes = size / (1024 * 1024)

    if megabytes >= 1024:
        return f"{megabytes / 1024:.1f}GB"

    return f"{megabytes:.0f}MB"


def collect_formats(info):
    duration = info.get("duration") or 0
    result = {}

    for item in info.get("formats") or []:
        height = item.get("height")
        video_codec = item.get("vcodec")

        if (
            not height
            or not video_codec
            or video_codec == "none"
            or height < 144
        ):
            continue

        size = (
            item.get("filesize")
            or item.get("filesize_approx")
        )

        if size is None:
            bitrate = item.get("tbr") or 0

            if bitrate and duration:
                size = int(bitrate * 1000 / 8 * duration)

        has_audio = (
            bool(item.get("acodec"))
            and item.get("acodec") != "none"
        )

        current = result.get(height)

        if current is None:
            result[height] = {
                "size": int(size or 0),
                "has_audio": has_audio,
            }
        else:
            if size and size > current["size"]:
                current["size"] = int(size)

            current["has_audio"] = (
                current["has_audio"] or has_audio
            )

    if not result:
        return {}

    heights = sorted(result)

    if len(heights) > MAX_FORMAT_BUTTONS:
        preferred = [
            height for height in STANDARD_HEIGHTS
            if height in result
        ]

        for height in reversed(heights):
            if len(preferred) >= MAX_FORMAT_BUTTONS:
                break

            if height not in preferred:
                preferred.append(height)

        preferred = sorted(preferred)[:MAX_FORMAT_BUTTONS]

        result = {
            height: result[height]
            for height in preferred
        }

    return result


def format_selector(kind, platform):
    if kind == "audio":
        return "bestaudio/best"

    if kind.isdigit():
        height = int(kind)

        if platform == "youtube":
            return (
                f"bv*[height<={height}]+ba/"
                f"b[height<={height}]"
            )

        return f"best[height<={height}]/best"

    if platform == "youtube":
        return "bv*+ba/b"

    return "best"


def fallback_chain(kind, platform, heights):
    if kind == "audio":
        return ["audio"]

    if kind.isdigit():
        selected_height = int(kind)

        lower = sorted(
            (
                height for height in (heights or [])
                if height < selected_height
            ),
            reverse=True,
        )

        return (
            [str(selected_height)]
            + [str(height) for height in lower]
            + ["audio"]
        )

    if platform == "youtube":
        return [kind] + YT_FALLBACKS.get(kind, [])

    if kind in SOCIAL_ORDER:
        return SOCIAL_ORDER[SOCIAL_ORDER.index(kind):]

    return list(SOCIAL_ORDER)


def label_for(kind):
    if kind.isdigit():
        return f"{kind}p"

    return LABELS.get(kind, kind)


def build_preview_text(info):
    title = html.escape(
        truncate(info.get("title") or "Видео", 70)
    )

    uploader = (
        info.get("uploader")
        or info.get("channel")
        or ""
    ).strip()

    uploader_url = (
        info.get("uploader_url")
        or info.get("channel_url")
        or ""
    ).strip()

    lines = [f"📼 <b>{title}</b> ➔"]

    if uploader:
        safe_uploader = html.escape(
            truncate(uploader, 40)
        )

        if uploader_url:
            safe_url = html.escape(
                uploader_url,
                quote=True,
            )

            lines.append(
                f'👤 <a href="{safe_url}">'
                f"{safe_uploader}</a> ➔"
            )
        else:
            lines.append(f"👤 {safe_uploader} ➔")

    formats = collect_formats(info)

    if formats:
        lines.append("")

        for height, data in formats.items():
            marker = "✅" if data["has_audio"] else "⚡"

            lines.append(
                f"{marker} {height}p: "
                f"{format_size(data['size'])}"
            )

    lines.extend([
        "",
        "Форматы для скачивания ↓",
    ])

    return "\n".join(lines), formats


def build_quality_keyboard(
    formats,
    selected,
    channel_url="",
    channel_name="канал",
):
    rows = []
    current_row = []

    for height, data in formats.items():
        marker = "✅" if data.get("has_audio") else "⚡"
        checkbox = "☑" if height in selected else "☐"

        current_row.append(
            InlineKeyboardButton(
                f"{marker} {height}p {checkbox}",
                callback_data=f"fmt:t:{height}",
            )
        )

        if len(current_row) == 3:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    rows.append([
        InlineKeyboardButton(
            "📼 MP3",
            callback_data="fmt:audio",
        ),
        InlineKeyboardButton(
            "🖼 Превью",
            callback_data="fmt:prev",
        ),
    ])

    rows.append([
        InlineKeyboardButton(
            "✂️ Обрезка",
            callback_data="fmt:trim",
        ),
        InlineKeyboardButton(
            "📝 Субтитры",
            callback_data="fmt:subs",
        ),
    ])

    if selected:
        rows.append([
            InlineKeyboardButton(
                "⬇️ Скачать выбранное",
                callback_data="fmt:dl",
            )
        ])
    else:
        best = max(formats) if formats else None

        rows.append([
            InlineKeyboardButton(
                (
                    f"⬇️ Скачать {best}p"
                    if best
                    else "⬇️ Скачать лучшее"
                ),
                callback_data="fmt:best",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            f"Перейти в 👤 {truncate(channel_name or 'канал', 30)} ➔",
            url=channel_url or CHANNEL_URL,
        )
    ])

    return InlineKeyboardMarkup(rows)


# ============================================================
# Общие утилиты
# ============================================================

def truncate(text, length):
    text = (text or "").strip()

    if len(text) <= length:
        return text

    return text[:length - 1].rstrip() + "…"


def format_duration(seconds):
    if not seconds:
        return ""

    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def human_size(size):
    if not size:
        return "?"

    megabytes = size / (1024 * 1024)

    if megabytes < 100:
        return f"{megabytes:.1f} MB"

    return f"{megabytes:.0f} MB"


def clean_error(error):
    return re.sub(
        r"\[\d+(;\d+)*m",
        "",
        str(error).replace("\x1b", ""),
    ).strip()


def format_uptime(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    return f"{hours}ч {minutes}м"


async def notify_admin(context, text):
    if not ADMIN_ID:
        return

    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"⚠️ {text}",
        )
    except Exception:
        log.warning(
            "Не удалось уведомить администратора",
            exc_info=True,
        )


def fetch_thumb_bytes(url, platform):
    if not url:
        return None

    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        if (
            PROXY_URL
            and platform == "youtube"
            and PROXY_URL.startswith("http")
        ):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({
                    "http": PROXY_URL,
                    "https": PROXY_URL,
                })
            )
        else:
            opener = urllib.request.build_opener()

        with opener.open(request, timeout=15) as response:
            return response.read() or None

    except Exception:
        log.warning(
            "Не удалось получить превью",
            exc_info=True,
        )
        return None


# ============================================================
# Прогресс скачивания
# ============================================================

def make_progress_bar(percent, length=10):
    filled = max(
        0,
        min(length, int(round(length * percent / 100))),
    )

    return "■" * filled + "□" * (length - filled)


class ProgressHook:
    def __init__(self, loop, updater, label):
        self.loop = loop
        self.updater = updater
        self.label = label

        self.last_time = 0.0
        self.last_percent = -1.0
        self.total = 0

    def post(self, text):
        try:
            asyncio.run_coroutine_threadsafe(
                self.updater(text),
                self.loop,
            )
        except Exception:
            pass

    def __call__(self, data):
        try:
            status = data.get("status")

            if status == "downloading":
                total = (
                    data.get("total_bytes")
                    or data.get("total_bytes_estimate")
                    or 0
                )

                if total:
                    self.total = total

                downloaded = data.get("downloaded_bytes") or 0

                percent = (
                    downloaded * 100.0 / self.total
                    if self.total
                    else 0.0
                )

                now = time.time()

                if (
                    now - self.last_time < PROGRESS_EVERY
                    and abs(percent - self.last_percent) < 5
                ):
                    return

                self.last_time = now
                self.last_percent = percent

                header = f"🎬 {self.label}"

                if self.total:
                    header += f" 💾 {human_size(self.total)}"

                body = (
                    "📥 Скачиваю:\n"
                    f"{make_progress_bar(percent)} {percent:.0f}%"
                )

                speed = (
                    data.get("_speed_str") or ""
                ).strip()

                if speed and speed != "N/A":
                    body += f"\n⚡ {speed}"

                self.post(f"{header}\n\n{body}")

            elif status == "finished":
                header = f"🎬 {self.label}"

                if self.total:
                    header += f" 💾 {human_size(self.total)}"

                self.post(
                    f"{header}\n\n🔄 Обрабатываю файл…"
                )

        except Exception:
            pass


# ============================================================
# FFmpeg и дополнительные инструменты
# ============================================================

def fix_aspect(path):
    if (
        not FIX_ASPECT
        or path.suffix.lower() not in (".mp4", ".mkv", ".mov")
    ):
        return path

    fixed = path.with_name(path.stem + "_sar.mp4")

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(path),
                "-c", "copy",
                "-bsf:v", "h264_metadata=sample_aspect_ratio=1/1",
                str(fixed),
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )

        if fixed.exists() and fixed.stat().st_size > 0:
            path.unlink(missing_ok=True)
            return fixed

    except Exception:
        fixed.unlink(missing_ok=True)

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(path),
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(fixed),
            ],
            capture_output=True,
            timeout=600,
            check=True,
        )

        if fixed.exists() and fixed.stat().st_size > 0:
            path.unlink(missing_ok=True)
            return fixed

    except Exception:
        log.warning(
            "Не удалось исправить соотношение сторон",
            exc_info=True,
        )
        fixed.unlink(missing_ok=True)

    return path


def trim_sync(path, start, end):
    output = path.with_name("trim_" + path.stem + ".mp4")

    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", start,
            "-to", end,
            "-i", str(path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output),
        ],
        capture_output=True,
        timeout=600,
        check=True,
    )

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg не создал файл")

    return output


def make_qr_sync(text):
    image = qrcode.make(text)

    path = (
        Path(tempfile.gettempdir())
        / f"qr_{uuid.uuid4().hex}.png"
    )

    image.save(path)
    return path


def tts_sync(text):
    mp3_path = (
        Path(tempfile.gettempdir())
        / f"tts_{uuid.uuid4().hex}.mp3"
    )

    gTTS(text=text, lang="ru").save(str(mp3_path))

    ogg_path = mp3_path.with_suffix(".ogg")

    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(mp3_path),
            "-c:a", "libopus",
            "-b:a", "64k",
            str(ogg_path),
        ],
        capture_output=True,
        timeout=180,
        check=True,
    )

    mp3_path.unlink(missing_ok=True)
    return ogg_path


def convert_sync(source, target):
    output = source.with_name(
        f"conv_{source.stem}.{target}"
    )

    common = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(source),
    ]

    if target == "mp3":
        command = common + [
            "-vn", "-c:a", "libmp3lame",
            "-b:a", "192k", str(output),
        ]
    elif target == "wav":
        command = common + [
            "-vn", "-c:a", "pcm_s16le",
            str(output),
        ]
    elif target == "m4a":
        command = common + [
            "-vn", "-c:a", "aac",
            "-b:a", "192k", str(output),
        ]
    else:
        command = common + [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-movflags", "+faststart",
            str(output),
        ]

    subprocess.run(
        command,
        capture_output=True,
        timeout=900,
        check=True,
    )

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("Конвертация не удалась")

    return output


def watermark_sync(source):
    output = source.with_name(
        "wm_" + source.stem + ".mp4"
    )

    video_filter = (
        f"drawtext=fontfile={FONT_PATH}:"
        f"text='{WATERMARK}':"
        "fontcolor=white@0.75:"
        "fontsize=h/22:"
        "box=1:"
        "boxcolor=black@0.35:"
        "boxborderw=10:"
        "x=w-tw-24:"
        "y=h-th-24"
    )

    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(source),
            "-vf", video_filter,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output),
        ],
        capture_output=True,
        timeout=900,
        check=True,
    )

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("Водяной знак не применён")

    return output


# ============================================================
# yt-dlp
# ============================================================

def base_opts(url, proxy=...):
    platform = platform_from_url(url)

    if proxy is ...:
        proxy = PROXY_URL

    options = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "socket_timeout": 25,
        "extractor_retries": 2,
        "file_access_retries": 2,
        "remote_components": ["ejs:github"],
        "extractor_args": {
            "youtube": {
                "player_client": ["default"],
            },
        },
    }

    if COOKIES_FILE and COOKIES_FILE.exists():
        options["cookiefile"] = str(COOKIES_FILE)

    if proxy and platform == "youtube":
        options["proxy"] = proxy

    return options


def ydl_options(url, kind, template, hook=None, proxy=...):
    platform = platform_from_url(url)

    options = base_opts(url, proxy)
    options["outtmpl"] = template

    if hook:
        options["progress_hooks"] = [hook]

    if kind == "audio":
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        options["format"] = format_selector(kind, platform)

        if platform != "youtube":
            options["format_sort"] = ["res", "br"]

        options["merge_output_format"] = "mp4"

    return options


def extract_info_sync(url, proxy=...):
    options = base_opts(url, proxy)
    options["skip_download"] = True

    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


def search_sync(query, count=5, proxy=...):
    if proxy is ...:
        proxy = PROXY_URL

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
    }

    if COOKIES_FILE and COOKIES_FILE.exists():
        options["cookiefile"] = str(COOKIES_FILE)

    if proxy:
        options["proxy"] = proxy

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            f"ytsearch{count}:{query}",
            download=False,
        )

    return [
        entry for entry in (info.get("entries") or [])
        if entry
    ]


def download_sync(url, kind, hook=None, proxy=...):
    platform = platform_from_url(url)

    job_dir = DOWNLOAD_DIR / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)

    template = str(
        job_dir / "%(title).90B [%(id)s].%(ext)s"
    )

    with yt_dlp.YoutubeDL(
        ydl_options(url, kind, template, hook, proxy)
    ) as ydl:
        info = ydl.extract_info(url, download=True)
        expected = Path(ydl.prepare_filename(info))

    if kind == "audio":
        mp3_files = list(job_dir.glob("*.mp3"))

        if mp3_files:
            return mp3_files[0]

    result = expected if expected.exists() else None

    if result is None:
        candidates = [
            path for path in job_dir.iterdir()
            if (
                path.is_file()
                and not path.name.endswith((".part", ".ytdl"))
            )
        ]

        if candidates:
            result = max(
                candidates,
                key=lambda path: path.stat().st_size,
            )

    if result is None:
        raise FileNotFoundError("Скачанный файл не найден")

    if platform in (
        "instagram",
        "facebook",
        "vk",
        "tiktok",
        "twitter",
        "rutube",
    ):
        result = fix_aspect(result)

    return result


async def download_with_proxies(url, kind, hook):
    platform = platform_from_url(url)
    last_error = None

    for proxy in proxy_candidates(platform):
        try:
            path = await asyncio.wait_for(
                run_io(download_sync, url, kind, hook, proxy),
                timeout=DOWNLOAD_TIMEOUT,
            )

            mark_proxy_success(proxy)
            return path

        except yt_dlp.utils.DownloadError as error:
            if proxy and is_proxy_error(error):
                mark_proxy_failed(proxy)
                last_error = error

                log.warning(
                    "Прокси %s не сработал, пробуем дальше",
                    mask_proxy(proxy),
                )
                continue

            raise

        except asyncio.TimeoutError:
            if proxy:
                mark_proxy_failed(proxy)
            raise

    if last_error:
        raise last_error

    raise yt_dlp.utils.DownloadError(
        "Нет доступных способов подключения"
    )


def download_subs_sync(url, proxy=...):
    job_dir = DOWNLOAD_DIR / ("subs_" + uuid.uuid4().hex)
    job_dir.mkdir(parents=True, exist_ok=True)

    options = base_opts(url, proxy)

    options.update({
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["ru", "en"],
        "subtitlesformat": "srt",
        "outtmpl": str(
            job_dir / "%(title).90B [%(id)s].%(ext)s"
        ),
    })

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(url, download=True)

    files = (
        list(job_dir.glob("*.srt"))
        + list(job_dir.glob("*.vtt"))
    )

    if not files:
        raise FileNotFoundError("Субтитры не найдены")

    return max(
        files,
        key=lambda path: path.stat().st_size,
    )


def cleanup_file(path):
    if path is None:
        return

    try:
        for item in path.parent.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)

        path.parent.rmdir()

    except OSError:
        pass


def cleanup_dir(directory):
    try:
        directory = Path(directory)

        for item in directory.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)

        directory.rmdir()

    except OSError:
        pass


# ============================================================
# Очередь скачивания
# ============================================================

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
stats_lock = asyncio.Lock()

active_jobs = 0
waiting_jobs = 0
completed_jobs = 0

started_at = time.time()


async def acquire_slot():
    global active_jobs, waiting_jobs

    async with stats_lock:
        waiting_jobs += 1

    await download_semaphore.acquire()

    async with stats_lock:
        waiting_jobs -= 1
        active_jobs += 1


async def release_slot(success):
    global active_jobs, completed_jobs

    async with stats_lock:
        active_jobs -= 1

        if success:
            completed_jobs += 1

    download_semaphore.release()


# ============================================================
# Команды
# ============================================================

def check_proxy_sync():
    pool = (
        ([PROXY_URL] if PROXY_URL else [])
        + PROXY_LIST
    )

    lines = [
        f"PROXY_URL = {mask_proxy(PROXY_URL) or '(пусто)'}"
    ]

    if not pool:
        return "\n".join(lines) + "\n\nПрокси не задан"

    for proxy in pool:
        started = time.time()

        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({
                    "http": proxy,
                    "https": proxy,
                })
            )

            request = urllib.request.Request(
                "https://www.youtube.com/",
                headers={"User-Agent": "Mozilla/5.0"},
            )

            with opener.open(request, timeout=15) as response:
                lines.append(
                    f"✅ {mask_proxy(proxy)} → "
                    f"{response.status} за "
                    f"{time.time() - started:.1f} сек."
                )

        except Exception as error:
            lines.append(
                f"❌ {mask_proxy(proxy)} → "
                f"{type(error).__name__}: {error}"
            )

    return "\n".join(lines)


async def cmd_checkproxy(update, context):
    if (
        not ADMIN_ID
        or update.effective_user.id != ADMIN_ID
    ):
        return

    message = await update.message.reply_text(
        "🌍 Проверяю прокси…"
    )

    result = await run_io(check_proxy_sync)
    await message.edit_text(result[:4000])


async def cmd_start(update, context):
    user = update.effective_user
    user_id = user.id

    stat_user(user_id, user.username or "")

    args = context.args or []
    payload = args[0] if args else ""

    async with _ref_lock:
        current = _refs.setdefault(str(user_id), {
            "invited": 0,
            "bonus": 0,
        })

        if payload.startswith("ref_"):
            owner = payload[4:]

            if (
                owner.isdigit()
                and int(owner) != user_id
                and not current.get("referred_by")
            ):
                current["referred_by"] = int(owner)

                owner_data = _refs.setdefault(owner, {
                    "invited": 0,
                    "bonus": 0,
                })

                owner_data["invited"] = (
                    owner_data.get("invited", 0) + 1
                )

                if owner_data["invited"] % REF_GOAL == 0:
                    owner_data["bonus"] = (
                        owner_data.get("bonus", 0) + 1
                    )

        await asyncio.to_thread(save_refs)

    if not await require_subscription(update, context):
        return

    cookies = (
        "есть"
        if COOKIES_FILE and COOKIES_FILE.exists()
        else "нет"
    )

    await update.message.reply_text(
        "👋 Привет! Отправь ссылку на видео — пришлю файл.\n\n"
        "▶️ YouTube  📸 Instagram  📘 Facebook\n"
        "🅥 VK  🎵 TikTok  🐦 Twitter/X\n"
        "📺 Rutube  🎧 SoundCloud\n\n"
        "✨ Умею:\n"
        "• 🎬 Скачивать видео\n"
        "• 🎵 Сохранять MP3\n"
        "• 🔍 Искать на YouTube\n"
        "• ⭐ Сохранять закладки\n"
        "• ✂️ Обрезать видео\n"
        "• 📝 Скачивать субтитры\n"
        "• 🛠 QR, TTS, конвертер и водяной знак\n\n"
        f"🍪 Cookies: {cookies}",
        reply_markup=REPLY_MENU,
    )


async def cmd_help(update, context):
    if not await require_subscription(update, context):
        return

    await update.message.reply_text(
        "🔗 Поддерживаемые ссылки:\n"
        "• youtube.com / youtu.be\n"
        "• instagram.com/reel\n"
        "• facebook.com / fb.watch\n"
        "• vk.com / vkvideo.ru\n"
        "• tiktok.com\n"
        "• twitter.com / x.com\n"
        "• rutube.ru/video\n"
        "• soundcloud.com\n\n"
        "⭐ Закладки — /saved\n"
        "🔍 Поиск — кнопка «🔍 Поиск»\n"
        "✂️ Обрезка — диапазон 00:10-00:40\n"
        "❌ Отмена — /cancel",
        reply_markup=REPLY_MENU,
    )


async def cmd_status(update, context):
    if not await require_subscription(update, context):
        return

    async with stats_lock:
        active = active_jobs
        waiting = waiting_jobs
        completed = completed_jobs

    invited, bonus = ref_stats(update.effective_user.id)

    cookies = (
        "✅ загружены"
        if COOKIES_FILE and COOKIES_FILE.exists()
        else "❌ нет"
    )

    await update.message.reply_text(
        "📊 Статус\n"
        f"Активных: {active}/{MAX_CONCURRENT}\n"
        f"В очереди: {waiting}\n"
        f"Скачано за сессию: {completed}\n"
        f"🍪 Cookies: {cookies}\n"
        f"🌍 Прокси: {mask_proxy(PROXY_URL) or '❌ выкл'}\n"
        f"🎬 yt-dlp: {yt_dlp.version.__version__}\n"
        f"Приглашено: {invited} (бонусов: {bonus})\n"
        f"Аптайм: {format_uptime(int(time.time() - started_at))}",
        reply_markup=REPLY_MENU,
    )


async def cmd_cancel(update, context):
    context.user_data.clear()

    await update.message.reply_text(
        "Все состояния сброшены.",
        reply_markup=REPLY_MENU,
    )


async def cmd_ref(update, context):
    if not await require_subscription(update, context):
        return

    user_id = update.effective_user.id
    invited, bonus = ref_stats(user_id)

    await update.message.reply_text(
        "🔗 Твоя реферальная ссылка:\n"
        f"{ref_link(context.bot.username, user_id)}\n\n"
        f"👥 Приглашено: {invited}\n"
        f"🎁 Бонусов: {bonus}\n\n"
        f"За каждые {REF_GOAL} приглашения — +1 бонус.",
        reply_markup=REPLY_MENU,
    )


async def cmd_saved(update, context):
    if not await require_subscription(update, context):
        return

    items = get_bookmarks(update.effective_user.id)

    if not items:
        await update.message.reply_text(
            "⭐ Пока пусто. Скачай видео — оно появится здесь.",
            reply_markup=REPLY_MENU,
        )
        return

    rows = []

    for index, item in enumerate(items[:BOOKMARKS_MAX]):
        platform = PLATFORM_NAMES.get(
            item.get("platform"),
            "🎬",
        )
        title = truncate(
            item.get("title", "Видео"),
            45,
        )

        rows.append([
            InlineKeyboardButton(
                f"{platform} {title}",
                callback_data=f"bk:{index}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "❌ Закрыть",
            callback_data="bk:close",
        )
    ])

    await update.message.reply_text(
        f"⭐ Твои закладки ({len(items)} из {BOOKMARKS_MAX}):",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_admin(update, context):
    if (
        not ADMIN_ID
        or update.effective_user.id != ADMIN_ID
    ):
        return

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    total_users = len(_stats["users"])
    downloads_today = _stats["downloads"].get(today, 0)
    total_downloads = sum(_stats["downloads"].values())

    top_platforms = sorted(
        _stats["platforms"].items(),
        key=lambda item: -item[1],
    )[:5]

    top_text = "\n".join(
        f"• {PLATFORM_NAMES.get(name, name)} — {count}"
        for name, count in top_platforms
    ) or "—"

    base = now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    last_seven_days = []

    for index in range(7):
        day = base - timedelta(days=index)
        day_string = day.strftime("%Y-%m-%d")

        last_seven_days.append(
            f"{day_string}: "
            f"{_stats['downloads'].get(day_string, 0)}"
        )

    await update.message.reply_text(
        "👨‍💼 Админ-панель\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"📥 Скачаний сегодня: {downloads_today}\n"
        f"📥 Всего: {total_downloads}\n"
        f"❌ Ошибок: {_stats['errors']}\n\n"
        f"🏆 Топ платформ:\n{top_text}\n\n"
        "📅 Последние 7 дней:\n"
        + "\n".join(f"• {item}" for item in last_seven_days),
        reply_markup=REPLY_MENU,
    )


# ============================================================
# Карточка видео
# ============================================================

async def show_video_menu(update, context, platform, url):
    message = update.effective_message

    if not message:
        return

    status_message = await message.reply_text(
        "🔍 Получаю информацию…"
    )

    info = None

    for proxy in proxy_candidates(platform):
        try:
            info = await asyncio.wait_for(
                run_ytdlp(extract_info_sync, url, proxy),
                timeout=60,
            )

            mark_proxy_success(proxy)
            break

        except yt_dlp.utils.DownloadError as error:
            if proxy and is_proxy_error(error):
                mark_proxy_failed(proxy)
                continue

            log.warning(
                "Не удалось получить информацию",
                exc_info=True,
            )
            break

        except asyncio.TimeoutError:
            log.warning("Таймаут получения информации")
            break

        except Exception:
            log.warning(
                "Ошибка получения информации",
                exc_info=True,
            )
            break

    context.user_data["url"] = url
    context.user_data["platform"] = platform
    context.user_data["selected"] = set()

    if info:
        context.user_data["title"] = truncate(
            info.get("title") or "Видео",
            80,
        )
        context.user_data["thumb_url"] = info.get("thumbnail")
        context.user_data["ch_url"] = (
            info.get("channel_url")
            or info.get("uploader_url")
            or ""
        )
        context.user_data["ch_name"] = (
            info.get("uploader")
            or info.get("channel")
            or "канал"
        )

        caption, formats = build_preview_text(info)
        context.user_data["formats"] = formats

    else:
        context.user_data["title"] = None
        context.user_data["thumb_url"] = None
        context.user_data["ch_url"] = ""
        context.user_data["ch_name"] = "канал"
        context.user_data["formats"] = {}

        caption = (
            f"{PLATFORM_NAMES.get(platform, '🎬 Видео')}\n\n"
            "Форматы для скачивания ↓"
        )

    context.user_data["heights"] = sorted(
        context.user_data["formats"].keys()
    )

    keyboard = build_quality_keyboard(
        context.user_data["formats"],
        set(),
        context.user_data["ch_url"],
        context.user_data["ch_name"],
    )

    try:
        await status_message.delete()
    except Exception:
        pass

    thumbnail = info.get("thumbnail") if info else None

    if thumbnail:
        data = await run_io(
            fetch_thumb_bytes,
            thumbnail,
            platform,
        )

        if data:
            try:
                await message.reply_photo(
                    photo=data,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                return
            except Exception:
                log.warning(
                    "Не удалось отправить карточку с фото",
                    exc_info=True,
                )

    await message.reply_text(
        caption,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ============================================================
# Поиск
# ============================================================

async def do_search(update, context, query):
    message = update.effective_message

    if not query or len(query) < 2:
        await message.reply_text(
            "Слишком короткий запрос.",
            reply_markup=REPLY_MENU,
        )
        return

    status_message = await message.reply_text(
        "🔍 Ищу на YouTube…"
    )

    try:
        results = await asyncio.wait_for(
            run_ytdlp(search_sync, query, 5),
            timeout=60,
        )
    except Exception:
        log.warning("Ошибка поиска", exc_info=True)

        await status_message.edit_text(
            "Не удалось выполнить поиск."
        )
        return

    if not results:
        await status_message.edit_text("Ничего не найдено.")
        return

    try:
        await status_message.delete()
    except Exception:
        pass

    context.user_data["search_results"] = []
    rows = []

    for index, entry in enumerate(results[:5]):
        title = truncate(
            entry.get("title") or "Видео",
            60,
        )

        video_id = entry.get("id")

        url = (
            f"https://www.youtube.com/watch?v={video_id}"
            if video_id
            else entry.get("url")
        )

        if not url:
            continue

        context.user_data["search_results"].append(url)

        result_index = (
            len(context.user_data["search_results"]) - 1
        )

        rows.append([
            InlineKeyboardButton(
                f"{index + 1}. {title[:55]}",
                callback_data=f"s:{result_index}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "❌ Отмена",
            callback_data="s:cancel",
        )
    ])

    await message.reply_text(
        "🔍 Нашёл на YouTube:\nНажми, чтобы скачать.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ============================================================
# Скачивание и отправка
# ============================================================

async def download_and_send(
    update,
    context,
    url,
    requested_kind,
    via_callback=False,
    trim=None,
    notify=True,
):
    query = update.callback_query if via_callback else None
    message = query.message if query else update.effective_message

    if not message:
        return

    chat_id = message.chat_id
    user_id = update.effective_user.id
    platform = platform_from_url(url)

    heights = context.user_data.get("heights") or []
    attempts = fallback_chain(
        requested_kind,
        platform,
        heights,
    )

    await acquire_slot()

    success = False
    loop = asyncio.get_running_loop()
    cookies_alerted = False

    async def edit_card(target_message, text):
        try:
            await context.bot.edit_message_caption(
                chat_id=target_message.chat_id,
                message_id=target_message.message_id,
                caption=text,
            )
        except Exception:
            try:
                await context.bot.edit_message_text(
                    chat_id=target_message.chat_id,
                    message_id=target_message.message_id,
                    text=text,
                )
            except Exception:
                pass

    try:
        for index, kind in enumerate(attempts):
            label = label_for(kind)

            heading = (
                f"🎬 {label}\n\n📥 Готовлю загрузку…"
                if index == 0
                else f"🎬 {label}\n\n⏳ Пробую снова…"
            )

            if via_callback:
                progress_message = query.message
                await edit_card(progress_message, heading)

                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=progress_message.chat_id,
                        message_id=progress_message.message_id,
                        reply_markup=None,
                    )
                except Exception:
                    pass
            else:
                progress_message = await message.reply_text(heading)

            async def update_progress(
                text,
                target=progress_message,
            ):
                await edit_card(target, text)

            hook = ProgressHook(
                loop,
                update_progress,
                label,
            )

            await context.bot.send_chat_action(
                chat_id,
                ChatAction.UPLOAD_VIDEO,
            )

            path = None

            try:
                path = await download_with_proxies(
                    url,
                    kind,
                    hook,
                )

                if trim:
                    try:
                        trimmed = await run_io(
                            trim_sync,
                            path,
                            trim[0],
                            trim[1],
                        )

                        cleanup_file(path)
                        path = trimmed

                    except Exception:
                        log.warning(
                            "Не удалось обрезать видео",
                            exc_info=True,
                        )

                        await context.bot.send_message(
                            chat_id,
                            "Не удалось обрезать. Отправляю полное видео.",
                        )

                size_mb = path.stat().st_size / (1024 * 1024)

                if size_mb > MAX_SIZE_MB:
                    cleanup_file(path)
                    path = None
                    continue

                await edit_card(
                    progress_message,
                    f"🎬 {label} "
                    f"💾 {human_size(path.stat().st_size)}\n\n"
                    "📤 Загружаю…",
                )

                with path.open("rb") as file_handle:
                    if kind == "audio":
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=file_handle,
                            title=path.stem,
                            caption="🎵 Готово!",
                        )
                    else:
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=file_handle,
                            supports_streaming=True,
                            caption=f"🎬 Готово! ({label})",
                        )

                success = True
                stat_download(platform)

                await add_bookmark(
                    user_id,
                    url,
                    context.user_data.get("title"),
                    platform,
                )

                if notify:
                    try:
                        await context.bot.delete_message(
                            progress_message.chat_id,
                            progress_message.message_id,
                        )
                    except Exception:
                        pass

                    await context.bot.send_message(
                        chat_id,
                        "✅ Готово! Сохранил в ⭐ Закладки.",
                        reply_markup=share_keyboard(
                            url,
                            context.bot.username or "bot",
                        ),
                    )
                else:
                    await edit_card(
                        progress_message,
                        f"✅ {label} — готово",
                    )

                return

            except yt_dlp.utils.DownloadError as error:
                error_text = str(error)

                log.warning(
                    "Формат %s: %s",
                    kind,
                    clean_error(error),
                )

                _stats["errors"] += 1

                if is_proxy_error(error):
                    log.warning(
                        "Прекращаем перебор форматов: ошибка прокси"
                    )
                    raise

                if (
                    not cookies_alerted
                    and (
                        "Sign in to confirm" in error_text
                        or "cookies" in error_text.lower()
                    )
                ):
                    cookies_alerted = True

                    await notify_admin(
                        context,
                        "Cookies YouTube устарели или отсутствуют.",
                    )

                try:
                    await context.bot.delete_message(
                        progress_message.chat_id,
                        progress_message.message_id,
                    )
                except Exception:
                    pass

                if index == len(attempts) - 1:
                    raise

            except asyncio.TimeoutError:
                log.warning(
                    "Таймаут скачивания формата %s",
                    kind,
                )

                _stats["errors"] += 1

                try:
                    await context.bot.delete_message(
                        progress_message.chat_id,
                        progress_message.message_id,
                    )
                except Exception:
                    pass

                if index == len(attempts) - 1:
                    raise

            finally:
                cleanup_file(path)

        await context.bot.send_message(
            chat_id,
            unavailable_message(),
            reply_markup=REPLY_MENU,
        )

    except yt_dlp.utils.DownloadError as error:
        log.warning(
            "Ошибка скачивания: %s",
            clean_error(error),
        )

        _stats["errors"] += 1

        if is_proxy_error(error):
            await context.bot.send_message(
                chat_id,
                "🌍 Прокси недоступен. Попробуй позже.",
                reply_markup=REPLY_MENU,
            )

            await notify_admin(
                context,
                "Проверь PROXY_URL или удали нерабочий прокси.",
            )
        else:
            await context.bot.send_message(
                chat_id,
                unavailable_message(),
                reply_markup=REPLY_MENU,
            )

    except asyncio.TimeoutError:
        _stats["errors"] += 1

        await context.bot.send_message(
            chat_id,
            "⏱ Загрузка заняла слишком много времени. "
            "Попробуй ещё раз.",
            reply_markup=REPLY_MENU,
        )

    except Exception:
        log.exception("Необработанная ошибка скачивания")
        _stats["errors"] += 1

        await context.bot.send_message(
            chat_id,
            unavailable_message(),
            reply_markup=REPLY_MENU,
        )

    finally:
        await release_slot(success)


# ============================================================
# Субтитры и превью
# ============================================================

async def send_subs(update, context, url):
    query = update.callback_query

    try:
        await query.edit_message_text("📝 Ищу субтитры…")
    except Exception:
        pass

    path = None

    try:
        path = await asyncio.wait_for(
            run_ytdlp(download_subs_sync, url),
            timeout=120,
        )

        with path.open("rb") as file_handle:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=file_handle,
                filename=path.name,
                caption="📝 Субтитры",
            )

        await context.bot.send_message(
            query.message.chat_id,
            "Что дальше?",
            reply_markup=REPLY_MENU,
        )

    except Exception:
        log.warning(
            "Не удалось получить субтитры",
            exc_info=True,
        )

        try:
            await query.edit_message_text(
                "❌ Субтитры не найдены."
            )
        except Exception:
            pass

    finally:
        cleanup_file(path)


async def send_preview_image(update, context):
    query = update.callback_query

    thumbnail = context.user_data.get("thumb_url")
    platform = context.user_data.get("platform", "other")

    title = truncate(
        context.user_data.get("title") or "Превью",
        100,
    )

    if not thumbnail:
        await context.bot.send_message(
            query.message.chat_id,
            "🖼 Превью недоступно.",
        )
        return

    data = await run_io(
        fetch_thumb_bytes,
        thumbnail,
        platform,
    )

    if not data:
        await context.bot.send_message(
            query.message.chat_id,
            "❌ Не удалось получить превью.",
        )
        return

    try:
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=data,
            caption=f"🖼 {title}",
        )
    except Exception:
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=io.BytesIO(data),
            filename="preview.jpg",
            caption=f"🖼 {title}",
        )


# ============================================================
# Обработка текстовых сообщений
# ============================================================

async def handle_text(update, context):
    if not await require_subscription(update, context):
        return

    text = (update.message.text or "").strip()
    state = context.user_data.get("await")

    if text == "❌ Отмена":
        context.user_data.clear()

        await update.message.reply_text(
            "Отменено.",
            reply_markup=REPLY_MENU,
        )
        return

    if text == "🎬 Скачать видео":
        context.user_data.pop("await", None)

        await update.message.reply_text(
            "Пришли ссылку на видео.",
            reply_markup=REPLY_MENU,
        )
        return

    if text == "🎵 MP3":
        context.user_data.pop("await", None)

        await update.message.reply_text(
            "Пришли ссылку, затем выбери MP3.",
            reply_markup=REPLY_MENU,
        )
        return

    if text == "🛠 Инструменты":
        context.user_data.pop("await", None)

        await update.message.reply_text(
            "🛠 Выбери инструмент:",
            reply_markup=TOOLS_MENU,
        )
        return

    if text == "🔍 Поиск":
        context.user_data["await"] = "search"

        await update.message.reply_text(
            "🔍 Что искать на YouTube?",
            reply_markup=REPLY_MENU,
        )
        return

    if text == "⭐ Закладки":
        await cmd_saved(update, context)
        return

    if text == "📊 Статус":
        await cmd_status(update, context)
        return

    if text == "🔗 Моя ссылка":
        await cmd_ref(update, context)
        return

    platform, url = detect_platform(text)

    if platform:
        context.user_data.clear()
        context.user_data["url"] = url
        context.user_data["platform"] = platform

        await show_video_menu(
            update,
            context,
            platform,
            url,
        )
        return

    if state == "qr":
        context.user_data.pop("await", None)

        if len(text) > 1000:
            await update.message.reply_text(
                "Слишком длинный текст для QR.",
                reply_markup=REPLY_MENU,
            )
            return

        status_message = await update.message.reply_text(
            "🔳 Создаю QR-код…"
        )

        path = None

        try:
            path = await run_io(make_qr_sync, text)

            with path.open("rb") as file_handle:
                await update.message.reply_photo(
                    photo=file_handle,
                    caption=f"🔳 QR-код: {truncate(text, 60)}",
                )

            await status_message.delete()

        except Exception:
            log.warning("Ошибка QR", exc_info=True)

            await status_message.edit_text(
                "❌ Не удалось создать QR-код."
            )

        finally:
            if path:
                path.unlink(missing_ok=True)

        await update.message.reply_text(
            "Что дальше?",
            reply_markup=REPLY_MENU,
        )
        return

    if state == "tts":
        context.user_data.pop("await", None)

        if len(text) > 800:
            await update.message.reply_text(
                "Слишком длинный текст. Максимум 800 символов.",
                reply_markup=REPLY_MENU,
            )
            return

        status_message = await update.message.reply_text(
            "🎙 Озвучиваю…"
        )

        path = None

        try:
            path = await run_io(tts_sync, text)

            with path.open("rb") as file_handle:
                await context.bot.send_voice(
                    chat_id=update.effective_chat.id,
                    voice=file_handle,
                    caption="🎙 Готово!",
                )

            await status_message.delete()

        except Exception:
            log.warning("Ошибка TTS", exc_info=True)

            await status_message.edit_text(
                "❌ Не удалось озвучить текст."
            )

        finally:
            if path:
                path.unlink(missing_ok=True)

        await update.message.reply_text(
            "Что дальше?",
            reply_markup=REPLY_MENU,
        )
        return

    if state == "search":
        context.user_data.pop("await", None)
        await do_search(update, context, text)
        return

    if state == "trim":
        trim_url = context.user_data.get("trim_url")

        match = re.match(
            r"^\s*"
            r"(\d{1,2}:\d{1,2}(?::\d{1,2})?)"
            r"\s*[-–—~]\s*"
            r"(\d{1,2}:\d{1,2}(?::\d{1,2})?)"
            r"\s*$",
            text,
        )

        context.user_data.pop("await", None)
        context.user_data.pop("trim_url", None)

        if not match or not trim_url:
            await update.message.reply_text(
                "Неверный диапазон. Пример: 00:10-00:40",
                reply_markup=REPLY_MENU,
            )
            return

        await download_and_send(
            update,
            context,
            trim_url,
            "best",
            trim=(match.group(1), match.group(2)),
        )
        return

    if state in ("conv_file", "wm_file"):
        context.user_data.pop("await", None)

        await update.message.reply_text(
            "Пришли файл документом.",
            reply_markup=REPLY_MENU,
        )
        return

    await do_search(update, context, text)


# ============================================================
# Документы
# ============================================================

async def on_document(update, context):
    if not await require_subscription(update, context):
        return

    document = update.message.document

    if not document:
        await update.message.reply_text(
            "Пришли файл документом.",
            reply_markup=REPLY_MENU,
        )
        return

    state = context.user_data.get("await")

    if state not in ("conv_file", "wm_file"):
        await update.message.reply_text(
            "Сначала выбери инструмент в меню «🛠 Инструменты».",
            reply_markup=REPLY_MENU,
        )
        return

    if (
        (document.file_size or 0)
        > MAX_UPLOAD_MB * 1024 * 1024
    ):
        await update.message.reply_text(
            f"❌ Файл больше {MAX_UPLOAD_MB} МБ.",
            reply_markup=REPLY_MENU,
        )
        return

    status_message = await update.message.reply_text(
        "📥 Скачиваю файл…"
    )

    job_dir = DOWNLOAD_DIR / ("doc_" + uuid.uuid4().hex)
    job_dir.mkdir(parents=True, exist_ok=True)

    source = job_dir / (
        document.file_name
        or f"file_{uuid.uuid4().hex}"
    )

    try:
        telegram_file = await document.get_file()
        await telegram_file.download_to_drive(str(source))

    except Exception:
        log.warning(
            "Не удалось скачать документ",
            exc_info=True,
        )

        await status_message.edit_text(
            "❌ Не удалось скачать файл."
        )

        cleanup_dir(job_dir)
        return

    if state == "conv_file":
        context.user_data["conv_src"] = str(source)
        context.user_data.pop("await", None)

        await status_message.edit_text(
            "🔄 В какой формат конвертировать?",
            reply_markup=CONVERT_MENU,
        )
        return

    context.user_data.pop("await", None)

    await status_message.edit_text(
        "💧 Накладываю водяной знак…"
    )

    output = None

    try:
        output = await run_io(watermark_sync, source)

        size_mb = output.stat().st_size / (1024 * 1024)

        if size_mb > MAX_SIZE_MB:
            await context.bot.send_message(
                update.effective_chat.id,
                f"❌ После обработки файл больше {MAX_SIZE_MB} МБ.",
            )
        else:
            with output.open("rb") as file_handle:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=file_handle,
                    caption=f"💧 Водяной знак: {WATERMARK}",
                )

        await status_message.delete()

    except Exception:
        log.warning(
            "Ошибка водяного знака",
            exc_info=True,
        )

        await status_message.edit_text(
            "❌ Не удалось наложить водяной знак."
        )

    finally:
        if output:
            output.unlink(missing_ok=True)

        cleanup_dir(job_dir)

    await update.message.reply_text(
        "Что дальше?",
        reply_markup=REPLY_MENU,
    )


# ============================================================
# Inline-кнопки
# ============================================================

async def on_callback(update, context):
    query = update.callback_query
    data = query.data or ""

    answered = False

    async def answer(*args, **kwargs):
        nonlocal answered

        if answered:
            return

        answered = True

        try:
            await query.answer(*args, **kwargs)
        except Exception:
            pass

    if data == "sub:check":
        subscribed = await check_subscription(
            context.bot,
            query.from_user.id,
            force=True,
        )

        if subscribed:
            await answer("Подписка подтверждена")

            await query.message.reply_text(
                "Спасибо! Теперь отправь ссылку.",
                reply_markup=REPLY_MENU,
            )
        else:
            await answer(
                "Сначала подпишись на канал.",
                show_alert=True,
            )
        return

    if data == "share:more":
        await answer("Пришли новую ссылку!")
        return

    # Закладки
    if data.startswith("bk:"):
        await answer()

        value = data[3:]

        if value == "close":
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        items = get_bookmarks(query.from_user.id)

        if (
            not value.isdigit()
            or int(value) >= len(items)
        ):
            try:
                await query.edit_message_text(
                    "Закладка устарела."
                )
            except Exception:
                pass
            return

        bookmark = items[int(value)]

        await show_video_menu(
            update,
            context,
            bookmark.get("platform", "other"),
            bookmark["url"],
        )
        return

    # Инструменты
    if data.startswith("tool:"):
        await answer()

        tool = data[5:]

        if tool == "qr":
            context.user_data["await"] = "qr"

            await query.edit_message_text(
                "🔳 Пришли текст или ссылку."
            )

        elif tool == "tts":
            context.user_data["await"] = "tts"

            await query.edit_message_text(
                "🎙 Пришли текст до 800 символов."
            )

        elif tool == "conv":
            context.user_data["await"] = "conv_file"

            await query.edit_message_text(
                "🔄 Пришли файл документом.\n"
                f"Лимит: {MAX_UPLOAD_MB} МБ."
            )

        elif tool == "wm":
            context.user_data["await"] = "wm_file"

            await query.edit_message_text(
                "💧 Пришли видео документом.\n"
                f"Лимит: {MAX_UPLOAD_MB} МБ."
            )

        elif tool == "ref":
            user_id = query.from_user.id
            invited, bonus = ref_stats(user_id)

            await query.edit_message_text(
                "🔗 Твоя ссылка:\n"
                f"{ref_link(context.bot.username, user_id)}\n\n"
                f"👥 Приглашено: {invited}\n"
                f"🎁 Бонусов: {bonus}"
            )
        return

    # Конвертер
    if data.startswith("conv:"):
        await answer()

        target = data[5:]

        if target == "cancel":
            context.user_data.pop("conv_src", None)

            try:
                await query.edit_message_text("❌ Отменено.")
            except Exception:
                pass
            return

        source_path = context.user_data.pop(
            "conv_src",
            None,
        )

        if (
            not source_path
            or not Path(source_path).exists()
        ):
            try:
                await query.edit_message_text(
                    "Файл устарел."
                )
            except Exception:
                pass
            return

        try:
            await query.edit_message_text(
                f"🔄 Конвертирую в {target.upper()}…"
            )
        except Exception:
            pass

        output = None

        try:
            output = await asyncio.wait_for(
                run_io(
                    convert_sync,
                    Path(source_path),
                    target,
                ),
                timeout=900,
            )

            size_mb = output.stat().st_size / (1024 * 1024)

            if size_mb > MAX_SIZE_MB:
                await context.bot.send_message(
                    query.message.chat_id,
                    f"❌ Результат больше {MAX_SIZE_MB} МБ.",
                )
            else:
                with output.open("rb") as file_handle:
                    if target in ("mp3", "wav", "m4a"):
                        await context.bot.send_audio(
                            chat_id=query.message.chat_id,
                            audio=file_handle,
                            title=output.stem,
                            caption=(
                                f"🔄 Готово ({target.upper()})"
                            ),
                        )
                    else:
                        await context.bot.send_video(
                            chat_id=query.message.chat_id,
                            video=file_handle,
                            caption=(
                                f"🔄 Готово ({target.upper()})"
                            ),
                        )

            await context.bot.send_message(
                query.message.chat_id,
                "Что дальше?",
                reply_markup=REPLY_MENU,
            )

        except Exception:
            log.warning(
                "Ошибка конвертации",
                exc_info=True,
            )

            await context.bot.send_message(
                query.message.chat_id,
                "❌ Не удалось конвертировать.",
            )

        finally:
            if output:
                output.unlink(missing_ok=True)

            cleanup_dir(Path(source_path).parent)

        return

    # Результаты поиска
    if data.startswith("s:"):
        await answer()

        value = data[2:]

        if value == "cancel":
            try:
                await query.edit_message_text(
                    "Поиск отменён."
                )
            except Exception:
                pass
            return

        results = (
            context.user_data.get("search_results")
            or []
        )

        if (
            not value.isdigit()
            or int(value) >= len(results)
        ):
            try:
                await query.edit_message_text(
                    "Результат устарел."
                )
            except Exception:
                pass
            return

        selected_url = results[int(value)]

        context.user_data["url"] = selected_url
        context.user_data["platform"] = "youtube"

        await show_video_menu(
            update,
            context,
            "youtube",
            selected_url,
        )
        return

    # Меню форматов
    if data.startswith("fmt:"):
        action = data[4:]

        if not await check_subscription(
            context.bot,
            query.from_user.id,
        ):
            await answer(
                "Сначала подпишись на канал.",
                show_alert=True,
            )

            await query.message.reply_text(
                f"Подпишись на {CHANNEL}.",
                reply_markup=subscription_keyboard(),
            )
            return

        formats = context.user_data.get("formats") or {}
        selected = context.user_data.setdefault(
            "selected",
            set(),
        )

        # Переключение галочки
        if action.startswith("t:"):
            try:
                height = int(action[2:])
            except ValueError:
                await answer()
                return

            if height in selected:
                selected.discard(height)
            else:
                selected.add(height)

            try:
                await query.edit_message_reply_markup(
                    reply_markup=build_quality_keyboard(
                        formats,
                        selected,
                        context.user_data.get("ch_url", ""),
                        context.user_data.get("ch_name", ""),
                    )
                )
            except Exception:
                pass

            await answer()
            return

        url = context.user_data.get("url")

        if not url:
            await answer(
                "Ссылка устарела. Отправь её ещё раз.",
                show_alert=True,
            )
            return

        if action == "prev":
            await answer()
            await send_preview_image(update, context)
            return

        if action == "subs":
            await answer()
            await send_subs(update, context, url)
            return

        if action == "trim":
            await answer()

            context.user_data["await"] = "trim"
            context.user_data["trim_url"] = url

            await context.bot.send_message(
                query.message.chat_id,
                "✂️ Пришли диапазон, например: 00:10-00:40",
                reply_markup=REPLY_MENU,
            )
            return

        if action == "audio":
            await answer()

            await download_and_send(
                update,
                context,
                url,
                "audio",
                via_callback=True,
            )
            return

        if action == "best":
            await answer()

            best = (
                str(max(formats))
                if formats
                else "best"
            )

            await download_and_send(
                update,
                context,
                url,
                best,
                via_callback=True,
            )
            return

        if action == "dl":
            if not selected:
                await answer(
                    "Ничего не выбрано.",
                    show_alert=True,
                )
                return

            await answer()

            items = sorted(selected)
            total = len(items)

            for index, height in enumerate(items):
                await download_and_send(
                    update,
                    context,
                    url,
                    str(height),
                    via_callback=True,
                    notify=(index == total - 1),
                )

            selected.clear()
            return

        await answer()
        return

    await answer()


# ============================================================
# Inline mode
# ============================================================

async def on_inline(update, context):
    query = update.inline_query
    text = (query.query or "").strip()

    platform, url = detect_platform(text)
    bot_username = context.bot.username or ""

    if not url:
        results = [
            InlineQueryResultArticle(
                id="hint",
                title="🎬 Пришлите ссылку",
                description=(
                    "YouTube, Instagram, VK, TikTok, Rutube"
                ),
                input_message_content=InputTextMessageContent(
                    f"🎬 Скачиваю видео в @{bot_username}"
                ),
            )
        ]

        await query.answer(results, cache_time=5)
        return

    results = [
        InlineQueryResultArticle(
            id="download",
            title=f"🎬 Скачать видео ({platform})",
            description="Открой бота и пришли ссылку",
            input_message_content=InputTextMessageContent(
                f"🎬 Скачать это видео:\n{url}\n\n"
                f"👉 @{bot_username}"
            ),
        )
    ]

    await query.answer(results, cache_time=5)


# ============================================================
# Health server для polling на Render Web Service
# ============================================================

def start_health_server():
    port = int(os.getenv("PORT", "0"))

    if not port:
        return

    class HealthHandler(BaseHTTPRequestHandler):
        def send_ok(self, include_body):
            body = b"OK - bot is running"

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8",
            )
            self.send_header(
                "Content-Length",
                str(len(body)),
            )
            self.end_headers()

            if include_body:
                self.wfile.write(body)

        def do_GET(self):
            self.send_ok(True)

        def do_HEAD(self):
            self.send_ok(False)

        def log_message(self, *args):
            pass

    HTTPServer(
        ("0.0.0.0", port),
        HealthHandler,
    ).serve_forever()


# ============================================================
# Дайджест
# ============================================================

async def digest_loop(application):
    while True:
        try:
            now = datetime.now(timezone.utc)

            target = now.replace(
                hour=DIGEST_HOUR,
                minute=0,
                second=0,
                microsecond=0,
            )

            if target <= now:
                target += timedelta(days=1)

            await asyncio.sleep(
                (target - now).total_seconds()
            )

            if not ADMIN_ID:
                continue

            day = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d"
            )

            downloads_today = _stats["downloads"].get(day, 0)
            total_downloads = sum(_stats["downloads"].values())

            top_platforms = sorted(
                _stats["platforms"].items(),
                key=lambda item: -item[1],
            )[:3]

            top_text = ", ".join(
                f"{PLATFORM_NAMES.get(name, name)} ({count})"
                for name, count in top_platforms
            ) or "—"

            async with stats_lock:
                active = active_jobs
                waiting = waiting_jobs

            await application.bot.send_message(
                ADMIN_ID,
                f"📊 Дайджест за {day}\n\n"
                f"📥 Скачано сегодня: {downloads_today}\n"
                f"📥 Всего: {total_downloads}\n"
                f"👥 Пользователей: {len(_stats['users'])}\n"
                f"❌ Ошибок: {_stats['errors']}\n"
                f"🏆 Топ: {top_text}\n"
                f"⚙️ Активно: {active}, в очереди: {waiting}",
            )

        except asyncio.CancelledError:
            log.info("Дайджест остановлен")
            raise

        except Exception:
            log.warning(
                "Ошибка дайджеста",
                exc_info=True,
            )
            await asyncio.sleep(3600)


_background_tasks = set()


def spawn_task(coroutine):
    task = asyncio.create_task(coroutine)

    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return task


# ============================================================
# Запуск и завершение
# ============================================================

async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "🏠 Меню"),
        BotCommand("saved", "⭐ Закладки"),
        BotCommand("help", "ℹ️ Помощь"),
        BotCommand("ref", "🔗 Моя ссылка"),
        BotCommand("status", "📊 Статус"),
        BotCommand("cancel", "❌ Отмена"),
    ])

    spawn_task(digest_loop(application))


async def post_shutdown(application):
    for task in list(_background_tasks):
        task.cancel()

    if _background_tasks:
        await asyncio.gather(
            *_background_tasks,
            return_exceptions=True,
        )

    _background_tasks.clear()
    log.info("Завершение работы выполнено")


async def on_error(update, context):
    error = context.error

    if error.__class__.__name__ == "Conflict":
        log.warning(
            "Conflict getUpdates: проверь, что запущен "
            "только один экземпляр бота"
        )
        return

    log.exception("Ошибка обработчика", exc_info=error)

    try:
        if (
            isinstance(update, Update)
            and update.effective_message
        ):
            await update.effective_message.reply_text(
                "⚠️ Внутренняя ошибка. Попробуй ещё раз.",
                reply_markup=REPLY_MENU,
            )
    except Exception:
        pass


def register_handlers(application):
    application.add_handler(
        CommandHandler("start", cmd_start)
    )
    application.add_handler(
        CommandHandler("help", cmd_help)
    )
    application.add_handler(
        CommandHandler("status", cmd_status)
    )
    application.add_handler(
        CommandHandler("cancel", cmd_cancel)
    )
    application.add_handler(
        CommandHandler("ref", cmd_ref)
    )
    application.add_handler(
        CommandHandler("saved", cmd_saved)
    )
    application.add_handler(
        CommandHandler("admin", cmd_admin)
    )
    application.add_handler(
        CommandHandler("checkproxy", cmd_checkproxy)
    )

    application.add_handler(
        InlineQueryHandler(on_inline)
    )

    application.add_handler(
        CallbackQueryHandler(
            on_callback,
            pattern=r"^(fmt:|sub:|s:|tool:|conv:|bk:|share:)",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            on_document,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    application.add_error_handler(on_error)


def main():
    if not TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")

    load_refs()
    load_bookmarks()

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .build()
    )

    register_handlers(application)

    log.info(
        "Запуск бота | yt-dlp %s | Режим %s | Прокси %s",
        yt_dlp.version.__version__,
        RUN_MODE,
        mask_proxy(PROXY_URL) or "выкл",
    )

    if RUN_MODE == "webhook":
        base_url = (
            os.getenv("RENDER_EXTERNAL_URL")
            or os.getenv("WEBHOOK_URL", "")
        ).rstrip("/")

        if not base_url:
            raise SystemExit(
                "Для webhook нужен RENDER_EXTERNAL_URL "
                "или WEBHOOK_URL"
            )

        application.run_webhook(
            listen="0.0.0.0",
            port=int(os.getenv("PORT", "10000")),
            url_path=WEBHOOK_PATH,
            webhook_url=f"{base_url}{WEBHOOK_PATH}",
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=DROP_PENDING,
        )
        return

    if os.getenv("PORT"):
        Thread(
            target=start_health_server,
            daemon=True,
        ).start()

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=DROP_PENDING,
    )


if __name__ == "__main__":
    main()
