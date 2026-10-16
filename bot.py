# bot.py — YouTube + Instagram + Facebook + VK Telegram bot
import os
import re
import time
import asyncio
import logging
import tempfile
import shutil
import uuid
import urllib.request
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

import yt_dlp
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ---------- Настройки ----------
TOKEN          = os.getenv("BOT_TOKEN")
MAX_SIZE_MB    = int(os.getenv("MAX_SIZE_MB", "48"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
PROXY_URL      = os.getenv("PROXY_URL", "").strip()
PROGRESS_EVERY = float(os.getenv("PROGRESS_EVERY", "3"))
FIX_ASPECT     = os.getenv("FIX_ASPECT", "0") == "1"

CHANNEL              = "@edrikevideo"
CHANNEL_URL          = "https://t.me/edrikevideo"
CONTACT_CHANNEL_TEXT = "напиши @edrikevideo"

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdl_bot"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ytdl-bot")

# ---------- Cookies ----------
LOCAL_COOKIES  = Path(__file__).parent / "cookies.txt"
SECRET_COOKIES = Path("/etc/secrets/cookies.txt")
TEMP_COOKIES   = Path(tempfile.gettempdir()) / "ytdl_bot_cookies.txt"

COOKIES_FILE = None
if SECRET_COOKIES.exists():
    try:
        shutil.copyfile(SECRET_COOKIES, TEMP_COOKIES)
        COOKIES_FILE = TEMP_COOKIES
    except OSError:
        log.exception("Не удалось скопировать Secret File cookies")
elif LOCAL_COOKIES.exists():
    COOKIES_FILE = LOCAL_COOKIES

log.info("Cookies доступны: %s", bool(COOKIES_FILE and COOKIES_FILE.exists()))
log.info("Прокси настроен: %s", bool(PROXY_URL))
log.info("Правка пропорций: %s", FIX_ASPECT)

# ---------- Платформы ----------
PLATFORM_PATTERNS = [
    ("youtube", re.compile(
        r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/[\w\-?=&/%.]+",
        re.IGNORECASE)),
    ("instagram", re.compile(
        r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv|share)/[\w\-?=&/%.]+",
        re.IGNORECASE)),
    ("facebook", re.compile(
        r"https?://(?:(?:www|m|web|ru-ru)\.)?(?:facebook\.com|fb\.watch|fb\.com)/[\w\-?=&/%.]+",
        re.IGNORECASE)),
    ("vk", re.compile(
        r"https?://(?:(?:m|new)\.)?(?:vk\.com|vkvideo\.ru|vk\.ru)/[\w\-?=&/%.]+",
        re.IGNORECASE)),
]

PLATFORM_NAMES = {
    "youtube": "▶️ YouTube",
    "instagram": "📸 Instagram",
    "facebook": "📘 Facebook",
    "vk": "🅥 VK",
}


def unavailable_message() -> str:
    return (
        "🛑 Ошибка скачивания!\n"
        "Этот видеоролик недоступен.\n"
        "Попробуй повторить запрос.\n"
        f"Если видео не загрузилось, {CONTACT_CHANNEL_TEXT}."
    )


def detect_platform(text: str):
    for platform, pattern in PLATFORM_PATTERNS:
        match = pattern.search(text)
        if match:
            return platform, match.group(0).rstrip(".,)")
    return None, None


def platform_from_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return "youtube"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram"
    if host == "facebook.com" or host.endswith(".facebook.com") or host.endswith(".fb.watch"):
        return "facebook"
    if host == "vk.com" or host.endswith(".vk.com") or host.endswith("vkvideo.ru") or host.endswith("vk.ru"):
        return "vk"
    return "other"


def fetch_thumb_bytes(url: str, platform: str):
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        if PROXY_URL and platform == "youtube" and PROXY_URL.startswith("http"):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({
                "http": PROXY_URL, "https": PROXY_URL,
            }))
        else:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=15) as resp:
            data = resp.read()
        return data if data else None
    except Exception:
        log.warning("Не удалось скачать превью: %s", url, exc_info=True)
        return None


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
        [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton("✅ Я подписался", callback_data="sub:check")],
    ])


async def require_subscription(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if await check_subscription(ctx.bot, update.effective_user.id):
        return True
    await update.effective_message.reply_text(
        "Чтобы пользоваться ботом, подпишитесь на канал @edrikevideo, "
        "затем нажмите «✅ Я подписался».",
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

QUALITY_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 1080p", callback_data="q:1080"),
     InlineKeyboardButton("🎬 720p",  callback_data="q:720")],
    [InlineKeyboardButton("🎬 480p",  callback_data="q:480"),
     InlineKeyboardButton("🎬 360p",  callback_data="q:360")],
    [InlineKeyboardButton("⭐ Лучшее", callback_data="q:best"),
     InlineKeyboardButton("🎵 MP3",    callback_data="q:audio")],
    [InlineKeyboardButton("⬅️ Назад",  callback_data="q:back"),
     InlineKeyboardButton("❌ Отмена", callback_data="q:cancel")],
])

YOUTUBE_MENU = QUALITY_MENU
SOCIAL_MENU  = QUALITY_MENU
VK_MENU      = QUALITY_MENU

QUALITY_LABELS = {
    "1080": "1080p", "720": "720p", "480": "480p",
    "360": "360p", "best": "лучшее качество", "audio": "MP3",
}

YOUTUBE_FORMATS = {
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720":  "bv*[height<=720]+ba/b[height<=720]",
    "480":  "bv*[height<=480]+ba/b[height<=480]",
    "360":  "bv*[height<=360]+ba/b[height<=360]",
    "best": "bv*+ba/b",
    "audio": "bestaudio/best",
}

# VK часто отдаёт отдельные видео- и аудиодорожки (HLS/DASH)
VK_FORMATS = {
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720":  "bv*[height<=720]+ba/b[height<=720]",
    "480":  "bv*[height<=480]+ba/b[height<=480]",
    "360":  "bv*[height<=360]+ba/b[height<=360]",
    "best": "bv*+ba/b",
    "audio": "bestaudio/best",
}

# Instagram/Facebook: обычно одна готовая дорожка с аудио
SOCIAL_FORMATS = {
    "1080": "best[height<=1080]/best[height<=1920]/best",
    "720":  "best[height<=720]/best[height<=1280]/best",
    "480":  "best[height<=480]/best",
    "360":  "best[height<=360]/best",
    "best": "best",
    "audio": "bestaudio/best",
}

YOUTUBE_FALLBACKS = {
    "1080": ["720", "480", "360", "audio"],
    "720":  ["480", "360", "audio"],
    "480":  ["360", "audio"],
    "360":  ["audio"],
    "best": ["720", "480", "360", "audio"],
    "audio": [],
}

SOCIAL_ORDER = ["1080", "720", "480", "360", "best", "audio"]


# ---------- Прогресс ----------
def make_bar(percent: float, length: int = 10) -> str:
    filled = max(0, min(length, int(round(length * percent / 100))))
    return "■" * filled + "□" * (length - filled)


def human_size(num_bytes) -> str:
    if not num_bytes:
        return "?"
    mb = num_bytes / (1024 * 1024)
    return f"{mb:.1f} MB" if mb < 100 else f"{mb:.0f} MB"


class ProgressHook:
    def __init__(self, loop, updater, label: str):
        self.loop = loop
        self.updater = updater
        self.label = label
        self.last_ts = 0.0
        self.last_percent = -1.0
        self.total = 0

    def _post(self, text: str):
        try:
            asyncio.run_coroutine_threadsafe(self.updater(text), self.loop)
        except Exception:
            log.debug("Не удалось поставить прогресс", exc_info=True)

    def __call__(self, d):
        try:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    self.total = total
                downloaded = d.get("downloaded_bytes") or 0
                if self.total:
                    percent = downloaded * 100.0 / self.total
                else:
                    try:
                        percent = float((d.get("_percent_str") or "0%").strip().rstrip("%"))
                    except Exception:
                        percent = 0.0

                now = time.time()
                if now - self.last_ts < PROGRESS_EVERY and abs(percent - self.last_percent) < 5:
                    return
                self.last_ts = now
                self.last_percent = percent

                bar = make_bar(percent)
                speed = (d.get("_speed_str") or "").strip()
                eta = (d.get("_eta_str") or "").strip()

                head = f"🎬 {self.label}"
                if self.total:
                    head += f" 💾 {human_size(self.total)}"
                body = f"📥 Скачиваю:\n{bar} {percent:.0f}%"
                if speed and speed != "N/A":
                    body += f"\n⚡ {speed}"
                if eta and eta != "N/A":
                    body += f"   ⏱ {eta}"
                self._post(f"{head}\n\n{body}")

            elif d.get("status") == "finished":
                head = f"🎬 {self.label}"
                if self.total:
                    head += f" 💾 {human_size(self.total)}"
                self._post(f"{head}\n\n🔄 Обрабатываю файл…")
        except Exception:
            log.debug("Ошибка progress hook", exc_info=True)


# ---------- Правка пропорций ----------
def fix_aspect(path: Path) -> Path:
    """Принудительно SAR=1:1 без перекодирования, чтобы телефон не растягивал."""
    if not FIX_ASPECT:
        return path
    if path.suffix.lower() not in (".mp4", ".mkv", ".mov"):
        return path

    fixed = path.with_name(path.stem + "_sar.mp4")

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
             "-c", "copy",
             "-bsf:v", "h264_metadata=sample_aspect_ratio=1/1",
             str(fixed)],
            capture_output=True, timeout=60, check=True,
        )
        if fixed.exists() and fixed.stat().st_size > 0:
            path.unlink(missing_ok=True)
            return fixed
    except Exception:
        log.info("SAR-фикс без перекодирования не сработал, пробую перекодировать")
        fixed.unlink(missing_ok=True)

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "copy", "-movflags", "+faststart",
             str(fixed)],
            capture_output=True, timeout=600, check=True,
        )
        if fixed.exists() and fixed.stat().st_size > 0:
            path.unlink(missing_ok=True)
            return fixed
    except Exception:
        log.warning("Не удалось исправить пропорции", exc_info=True)
        fixed.unlink(missing_ok=True)

    return path


# ---------- yt-dlp ----------
def ydl_options(url: str, kind: str, output_template: str, hook=None) -> dict:
    platform = platform_from_url(url)
    opts = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 20,
        "progress_hooks": [hook] if hook else [],
    }
    if COOKIES_FILE and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if PROXY_URL and platform == "youtube":
        opts["proxy"] = PROXY_URL

    if kind == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif platform == "youtube":
        opts["format"] = YOUTUBE_FORMATS.get(kind, YOUTUBE_FORMATS["best"])
        opts["merge_output_format"] = "mp4"
    elif platform == "vk":
        opts["format"] = VK_FORMATS.get(kind, VK_FORMATS["best"])
        opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = SOCIAL_FORMATS.get(kind, SOCIAL_FORMATS["best"])
        opts["format_sort"] = ["res", "br"]
        opts["merge_output_format"] = "mp4"
    return opts


def extract_info_sync(url: str) -> dict:
    platform = platform_from_url(url)
    opts = {
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
    }
    if COOKIES_FILE and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if PROXY_URL and platform == "youtube":
        opts["proxy"] = PROXY_URL
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_sync(url: str, kind: str, hook=None) -> Path:
    platform = platform_from_url(url)
    job_dir = DOWNLOAD_DIR / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)
    template = str(job_dir / "%(title).100B [%(id)s].%(ext)s")

    with yt_dlp.YoutubeDL(ydl_options(url, kind, template, hook)) as ydl:
        info = ydl.extract_info(url, download=True)
        expected = Path(ydl.prepare_filename(info))

    if kind == "audio":
        mp3_files = list(job_dir.glob("*.mp3"))
        if mp3_files:
            return mp3_files[0]

    result = expected if expected.exists() else None
    if result is None:
        candidates = [p for p in job_dir.iterdir()
                      if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if candidates:
            result = max(candidates, key=lambda p: p.stat().st_size)

    if result is None:
        raise FileNotFoundError("После скачивания файл не найден")

    if platform in ("instagram", "facebook", "vk"):
        result = fix_aspect(result)

    return result


def cleanup_file(path):
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


# ---------- Утилиты ----------
def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def format_duration(seconds) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def clean_error(error: Exception) -> str:
    text = str(error).replace("\x1b", "")
    return re.sub(r"\[\d+(;\d+)*m", "", text).strip()


def format_uptime(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}ч {m}м"


semaphore = asyncio.Semaphore(MAX_CONCURRENT)
stats_lock = asyncio.Lock()
active_jobs = 0
waiting_jobs = 0
completed_jobs = 0
started_at = time.time()


async def acquire_slot():
    global active_jobs, waiting_jobs
    async with stats_lock:
        waiting_jobs += 1
    await semaphore.acquire()
    async with stats_lock:
        waiting_jobs -= 1
        active_jobs += 1


async def release_slot(success: bool):
    global active_jobs, completed_jobs
    async with stats_lock:
        active_jobs -= 1
        if success:
            completed_jobs += 1
    semaphore.release()


# ---------- Команды ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx):
        return
    cookies_note = "есть" if COOKIES_FILE and COOKIES_FILE.exists() else "нет"
    proxy_note = "включён для YouTube" if PROXY_URL else "выключен"
    await update.message.reply_text(
        "👋 Привет! Отправьте ссылку на видео из:\n"
        "▶️ YouTube  •  📸 Instagram  •  📘 Facebook  •  🅥 VK\n\n"
        f"Лимит Telegram: около {MAX_SIZE_MB} МБ.\n"
        f"Cookies: {cookies_note}.\n"
        f"Прокси: {proxy_note}.",
        reply_markup=REPLY_MENU,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx):
        return
    await update.message.reply_text(
        "Отправьте ссылку — покажу доступные варианты качества.\n\n"
        "Поддерживаются:\n"
        "• YouTube — видео, Shorts\n"
        "• Instagram — Reels, посты\n"
        "• Facebook — видео, fb.watch\n"
        "• VK — видео, клипы\n\n"
        "Качество: 1080p / 720p / 480p / 360p / MP3.\n"
        "Большие видео автоматически понижаются по размеру.",
        reply_markup=REPLY_MENU,
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx):
        return
    async with stats_lock:
        a, w, c = active_jobs, waiting_jobs, completed_jobs
    cookies_note = "✅ загружены" if COOKIES_FILE and COOKIES_FILE.exists() else "❌ нет"
    proxy_note = "✅ вкл (YouTube)" if PROXY_URL else "❌ выкл"
    await update.message.reply_text(
        f"📊 Статус\n"
        f"Активных задач: {a}/{MAX_CONCURRENT}\n"
        f"В очереди: {w}\n"
        f"Успешно отправлено: {c}\n"
        f"Cookies: {cookies_note}\n"
        f"Прокси: {proxy_note}\n"
        f"Аптайм: {format_uptime(int(time.time() - started_at))}",
        reply_markup=REPLY_MENU,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("url", None)
    ctx.user_data.pop("platform", None)
    await update.message.reply_text("Отменено.", reply_markup=REPLY_MENU)


# ---------- Обработка ссылки ----------
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx):
        return

    text = (update.message.text or "").strip()
    if text == "🎬 Скачать видео":
        await update.message.reply_text("Пришлите ссылку на видео.", reply_markup=REPLY_MENU); return
    if text == "🎵 Скачать MP3":
        await update.message.reply_text("Пришлите ссылку; затем выберите MP3.", reply_markup=REPLY_MENU); return
    if text == "ℹ️ Помощь":
        await cmd_help(update, ctx); return
    if text == "📊 Статус":
        await cmd_status(update, ctx); return
    if text == "❌ Отмена":
        await cmd_cancel(update, ctx); return

    platform, url = detect_platform(text)
    if not platform:
        await update.message.reply_text(
            "Не распознал ссылку.\n"
            "Поддерживаются: YouTube, Instagram, Facebook, VK.",
            reply_markup=REPLY_MENU,
        )
        return

    ctx.user_data["url"] = url
    ctx.user_data["platform"] = platform

    info_message = await update.message.reply_text("🔍 Получаю информацию…")
    loop = asyncio.get_running_loop()

    info = None
    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(None, extract_info_sync, url),
            timeout=40,
        )
    except asyncio.TimeoutError:
        log.warning("Таймаут при получении информации")
    except Exception:
        log.warning("Не удалось получить информацию", exc_info=True)

    if info:
        title = truncate(info.get("title") or "Видео", 60)
        uploader = truncate(info.get("uploader") or info.get("channel") or "", 40)
        duration = format_duration(info.get("duration"))
        lines = [f"{PLATFORM_NAMES[platform]}: {title}"]
        if uploader: lines.append(f"👤 {uploader}")
        if duration: lines.append(f"⏱ {duration}")
    else:
        lines = [f"{PLATFORM_NAMES[platform]}: видео"]

    lines.append("")
    lines.append("Выберите качество:")
    caption = "\n".join(lines)
    keyboard = QUALITY_MENU

    await info_message.delete()

    thumb = info.get("thumbnail") if info else None
    thumb_bytes = None
    if thumb:
        try:
            thumb_bytes = await loop.run_in_executor(
                None, fetch_thumb_bytes, thumb, platform
            )
        except Exception:
            log.warning("Не удалось подготовить превью", exc_info=True)

    if thumb_bytes:
        try:
            await update.message.reply_photo(
                photo=thumb_bytes, caption=caption, reply_markup=keyboard
            )
            return
        except Exception:
            log.warning("Не отправить превью, отправляю текстом")

    await update.message.reply_text(caption, reply_markup=keyboard)


# ---------- Кнопки ----------
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    if data == "sub:check":
        if await check_subscription(ctx.bot, query.from_user.id):
            await query.answer("Подписка подтверждена")
            await query.message.reply_text("Спасибо! Теперь отправьте ссылку.", reply_markup=REPLY_MENU)
        else:
            await query.answer("Сначала подпишитесь на канал.", show_alert=True)
        return

    await query.answer()
    action = data.removeprefix("q:")

    if action in ("back", "cancel"):
        ctx.user_data.pop("url", None)
        ctx.user_data.pop("platform", None)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            await query.edit_message_text(
                "⬅️ Пришлите новую ссылку." if action == "back" else "❌ Отменено."
            )
        except Exception:
            pass
        return

    if not await check_subscription(ctx.bot, query.from_user.id):
        await query.message.reply_text(
            "Чтобы пользоваться ботом, подпишитесь на канал @edrikevideo.",
            reply_markup=subscription_keyboard(),
        )
        return

    url = ctx.user_data.get("url")
    if not url:
        try:
            await query.edit_message_text("Ссылка устарела. Пришлите её ещё раз.")
        except Exception:
            pass
        return

    await download_and_send(update, ctx, url, action, via_callback=True)


# ---------- Скачивание с прогрессом ----------
async def download_and_send(update, ctx, url, requested_kind, via_callback=False):
    query = update.callback_query if via_callback else None
    message = query.message if query else update.effective_message
    chat_id = message.chat_id
    platform = platform_from_url(url)

    if platform == "youtube":
        attempts = [requested_kind] + YOUTUBE_FALLBACKS.get(requested_kind, [])
    elif requested_kind == "audio":
        attempts = ["audio"]
    else:
        if requested_kind in SOCIAL_ORDER:
            start = SOCIAL_ORDER.index(requested_kind)
            attempts = SOCIAL_ORDER[start:]
        else:
            attempts = list(SOCIAL_ORDER)

    await acquire_slot()
    success = False
    loop = asyncio.get_running_loop()

    try:
        for index, kind in enumerate(attempts):
            label = QUALITY_LABELS.get(kind, kind)
            head = f"🎬 {label}\n\n📥 Готовлю загрузку…"
            if index:
                head = f"🎬 {label}\n\n⏳ Предыдущий формат не подошёл, пробую снова…"

            if via_callback:
                try:
                    await query.edit_message_text(head)
                    progress_msg = query.message
                except Exception:
                    progress_msg = await ctx.bot.send_message(chat_id, head)
            else:
                progress_msg = await message.reply_text(head)

            async def update_progress(text: str, _msg=progress_msg):
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=_msg.chat_id,
                        message_id=_msg.message_id,
                        text=text,
                    )
                except Exception:
                    pass

            hook = ProgressHook(loop, update_progress, label)
            await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)

            path = None
            try:
                path = await loop.run_in_executor(None, download_sync, url, kind, hook)
                size_mb = path.stat().st_size / (1024 * 1024)

                if size_mb > MAX_SIZE_MB:
                    cleanup_file(path)
                    path = None
                    continue

                try:
                    await ctx.bot.edit_message_text(
                        chat_id=progress_msg.chat_id,
                        message_id=progress_msg.message_id,
                        text=f"🎬 {label} 💾 {human_size(path.stat().st_size)}\n\n📤 Загружаю в Telegram…",
                    )
                except Exception:
                    pass

                with path.open("rb") as media:
                    if kind == "audio":
                        await ctx.bot.send_audio(
                            chat_id=chat_id, audio=media,
                            title=path.stem, caption="🎵 Готово!",
                        )
                    else:
                        await ctx.bot.send_video(
                            chat_id=chat_id, video=media,
                            supports_streaming=True,
                            caption=f"🎬 Готово! ({label})",
                        )

                success = True
                try:
                    await ctx.bot.delete_message(progress_msg.chat_id, progress_msg.message_id)
                except Exception:
                    pass

                await ctx.bot.send_message(chat_id, "Что дальше?", reply_markup=REPLY_MENU)
                return

            except yt_dlp.utils.DownloadError as error:
                log.warning("Формат %s недоступен: %s", kind, clean_error(error))
                try:
                    await ctx.bot.delete_message(progress_msg.chat_id, progress_msg.message_id)
                except Exception:
                    pass
                if index == len(attempts) - 1:
                    raise
            finally:
                cleanup_file(path)

        await ctx.bot.send_message(chat_id, unavailable_message(), reply_markup=REPLY_MENU)

    except yt_dlp.utils.DownloadError as error:
        log.warning("Не удалось скачать: %s", clean_error(error))
        await ctx.bot.send_message(chat_id, unavailable_message(), reply_markup=REPLY_MENU)
    except Exception:
        log.exception("Ошибка скачивания")
        await ctx.bot.send_message(chat_id, unavailable_message(), reply_markup=REPLY_MENU)
    finally:
        await release_slot(success)


# ---------- HTTP health check ----------
def start_health_server():
    port = int(os.getenv("PORT", "0"))
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def _send_ok(self, with_body: bool):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "19")
            self.end_headers()
            if with_body:
                self.wfile.write(b"OK - bot is running")

        def do_GET(self):
            self._send_ok(with_body=True)

        def do_HEAD(self):
            self._send_ok(with_body=False)

        def log_message(self, *_args):
            pass

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ---------- Запуск ----------
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Меню"),
        BotCommand("help", "Помощь"),
        BotCommand("status", "Статус"),
        BotCommand("cancel", "Отмена"),
    ])


def main():
    if not TOKEN:
        raise SystemExit("Не задана переменная BOT_TOKEN.")
    if os.getenv("PORT"):
        Thread(target=start_health_server, daemon=True).start()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(q:|sub:)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Бот запускается. Прокси: %s", bool(PROXY_URL))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
