# bot.py — YouTube / Insta / FB / VK / TikTok / Twitter / Rutube / SoundCloud
# + закладки + share + админ-панель + авто-дайджест + поиск + inline
# + обрезка + субтитры + рефералка + QR + TTS + конвертер + водяной знак
import os
import sys
import signal
from types import SimpleNamespace
import re
import time
import json
import uuid
import asyncio
import logging
import tempfile
import shutil
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, quote
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone, timedelta
from contextlib import suppress

import yt_dlp
import qrcode
from gtts import gTTS
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand,
    InlineQueryResultArticle, InputTextMessageContent,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, InlineQueryHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

# ========== Настройки ==========
TOKEN          = os.getenv("BOT_TOKEN")
MAX_SIZE_MB    = int(os.getenv("MAX_SIZE_MB", "48"))
MAX_CONCURRENT = max(1, int(os.getenv("MAX_CONCURRENT", "2")))
PROXY_URL      = os.getenv("PROXY_URL", "").strip()
PROGRESS_EVERY = float(os.getenv("PROGRESS_EVERY", "3"))
FIX_ASPECT     = os.getenv("FIX_ASPECT", "0") == "1"
REF_GOAL       = max(1, int(os.getenv("REF_GOAL", "3")))
WATERMARK      = os.getenv("WATERMARK", "@edrikevideo")
FONT_PATH      = os.getenv("FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
MAX_UPLOAD_MB  = 20
BOOKMARKS_MAX  = 10
DIGEST_HOUR    = int(os.getenv("DIGEST_HOUR", "20"))  # UTC, 20:00 UTC = 23:00 МСК

CHANNEL      = "@edrikevideo"
CHANNEL_URL  = "https://t.me/edrikevideo"
CONTACT_TEXT = "напиши @edrikevideo"
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdl_bot"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
# DATA_DIR must point to a mounted persistent disk to survive redeployments.
DATA_DIR = Path(os.getenv("DATA_DIR", tempfile.gettempdir()))
DATA_DIR.mkdir(parents=True, exist_ok=True)
REF_FILE = DATA_DIR / "ytdl_refs.json"
BK_FILE = DATA_DIR / "ytdl_bookmarks.json"


def atomic_write_json(path, data):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ytdl-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# HTTP client INFO logs may include the Telegram token in request URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

JOB_TIMEOUT = max(10, int(os.getenv("JOB_TIMEOUT", "240")))
QUEUE_LIMIT = max(1, int(os.getenv("QUEUE_LIMIT", "10")))

# ========== Cookies ==========
LOCAL_COOKIES  = Path(__file__).parent / "cookies.txt"
SECRET_COOKIES = Path("/etc/secrets/cookies.txt")
TEMP_COOKIES   = Path(tempfile.gettempdir()) / "ytdl_bot_cookies.txt"

COOKIES_FILE = None
if SECRET_COOKIES.exists():
    try:
        shutil.copyfile(SECRET_COOKIES, TEMP_COOKIES)
        COOKIES_FILE = TEMP_COOKIES
    except OSError:
        log.exception("cookies copy failed")
elif LOCAL_COOKIES.exists():
    COOKIES_FILE = LOCAL_COOKIES

log.info("Cookies: %s | Прокси: %s", bool(COOKIES_FILE and COOKIES_FILE.exists()), bool(PROXY_URL))

# ========== Рефералка ==========
_refs = {}
_ref_lock = asyncio.Lock()


def load_refs():
    global _refs
    try:
        if REF_FILE.exists():
            _refs = json.loads(REF_FILE.read_text(encoding="utf-8"))
    except Exception:
        _refs = {}


def save_refs():
    try:
        atomic_write_json(REF_FILE, _refs)
    except Exception:
        log.warning("refs save failed", exc_info=True)


def ref_link(bot_username, uid):
    return f"https://t.me/{bot_username}?start=ref_{uid}"


def ref_stats(uid):
    d = _refs.get(str(uid), {})
    return d.get("invited", 0), d.get("bonus", 0)


# ========== Закладки ==========
_bookmarks = {}
_bk_lock = asyncio.Lock()


def load_bookmarks():
    global _bookmarks
    try:
        if BK_FILE.exists():
            _bookmarks = json.loads(BK_FILE.read_text(encoding="utf-8"))
    except Exception:
        _bookmarks = {}


def save_bookmarks():
    try:
        atomic_write_json(BK_FILE, _bookmarks)
    except Exception:
        log.warning("bookmarks save failed", exc_info=True)


async def add_bookmark(uid, url, title, platform):
    async with _bk_lock:
        lst = _bookmarks.setdefault(str(uid), [])
        lst = [b for b in lst if b.get("url") != url]
        lst.insert(0, {
            "url": url,
            "title": (title or "Видео")[:80],
            "platform": platform,
            "ts": int(time.time()),
        })
        _bookmarks[str(uid)] = lst[:BOOKMARKS_MAX]
        save_bookmarks()


def get_bookmarks(uid):
    return _bookmarks.get(str(uid), [])


# ========== Статистика ==========
_stats = {
    "users": {},
    "downloads": {},
    "platforms": {},
    "errors": 0,
    "started": int(time.time()),
}


def stat_user(uid, username=""):
    u = _stats["users"].setdefault(str(uid), {"first_seen": int(time.time()), "username": username})
    if username:
        u["username"] = username


def stat_download(platform):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _stats["downloads"][day] = _stats["downloads"].get(day, 0) + 1
    _stats["platforms"][platform] = _stats["platforms"].get(platform, 0) + 1


# ========== Платформы ==========
PLATFORM_PATTERNS = [
    ("youtube",    re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/[\w\-?=&/%.]+", re.I)),
    ("instagram",  re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv|share)/[\w\-?=&/%.]+", re.I)),
    ("facebook",   re.compile(r"https?://(?:(?:www|m|web|ru-ru)\.)?(?:facebook\.com|fb\.watch|fb\.com)/[\w\-?=&/%.]+", re.I)),
    ("vk",         re.compile(r"https?://(?:(?:m|new)\.)?(?:vk\.com|vkvideo\.ru|vk\.ru)/[\w\-?=&/%.]+", re.I)),
    ("tiktok",     re.compile(r"https?://(?:www\.|m\.)?(?:tiktok\.com|vt\.tiktok\.com)/[\w\-?=&/%.@]+", re.I)),
    ("twitter",    re.compile(r"https?://(?:(?:www\.|mobile\.)?(?:twitter\.com|x\.com))/[\w\-?=&/%.]+", re.I)),
    ("rutube",     re.compile(r"https?://(?:www\.)?rutube\.ru/(?:video|shorts)/[\w\-?=&/%.]+", re.I)),
    ("soundcloud", re.compile(r"https?://(?:www\.|m\.)?soundcloud\.com/[\w\-?=&/%.]+", re.I)),
]

PLATFORM_NAMES = {
    "youtube": "▶️ YouTube", "instagram": "📸 Instagram", "facebook": "📘 Facebook",
    "vk": "🅥 VK", "tiktok": "🎵 TikTok", "twitter": "🐦 Twitter / X",
    "rutube": "📺 Rutube", "soundcloud": "🎧 SoundCloud",
}


def unavailable_message():
    return ("🛑 Ошибка скачивания!\n"
            "Этот видеоролик недоступен.\n"
            "Попробуй повторить запрос.\n"
            f"Если видео не загрузилось, {CONTACT_TEXT}.")


def detect_platform(text):
    for p, rx in PLATFORM_PATTERNS:
        m = rx.search(text or "")
        if m:
            return p, m.group(0).rstrip(".,)")
    return None, None


def platform_from_url(url):
    h = (urlsplit(url).hostname or "").lower()
    if h in ("youtu.be", "youtube.com") or h.endswith(".youtube.com"):
        return "youtube"
    if h == "instagram.com" or h.endswith(".instagram.com"):
        return "instagram"
    if h == "fb.watch" or h.endswith(".fb.watch") or h.endswith("facebook.com"):
        return "facebook"
    if h.endswith("vk.com") or h.endswith("vk.ru") or h.endswith("vkvideo.ru"):
        return "vk"
    if h.endswith("tiktok.com"):
        return "tiktok"
    if h.endswith("twitter.com") or h.endswith("x.com"):
        return "twitter"
    if h.endswith("rutube.ru"):
        return "rutube"
    if h.endswith("soundcloud.com"):
        return "soundcloud"
    return "other"


def fetch_thumb_bytes(url, platform):
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        if PROXY_URL and platform == "youtube" and PROXY_URL.startswith("http"):
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}))
        else:
            op = urllib.request.build_opener()
        with op.open(req, timeout=15) as r:
            return r.read() or None
    except Exception:
        log.warning("thumb failed", exc_info=True)
        return None


# ========== Подписка ==========
async def check_subscription(bot, user_id):
    try:
        m = await bot.get_chat_member(CHANNEL, user_id)
        return m.status not in ("left", "kicked")
    except Exception:
        log.exception("sub check failed")
        return False


def sub_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton("✅ Я подписался", callback_data="sub:check")],
    ])


async def require_subscription(update, ctx):
    u = update.effective_user
    stat_user(u.id, u.username or "")
    if await check_subscription(ctx.bot, u.id):
        return True
    await update.effective_message.reply_text(
        f"Чтобы пользоваться ботом, подпишитесь на {CHANNEL} и нажмите «✅ Я подписался».",
        reply_markup=sub_keyboard())
    return False


# ========== Меню ==========
REPLY_MENU = ReplyKeyboardMarkup([
    [KeyboardButton("🎬 Скачать видео"), KeyboardButton("🔍 Поиск")],
    [KeyboardButton("🎵 MP3"),           KeyboardButton("🛠 Инструменты")],
    [KeyboardButton("⭐ Закладки"),      KeyboardButton("📊 Статус")],
    [KeyboardButton("🔗 Моя ссылка"),    KeyboardButton("❌ Отмена")],
], resize_keyboard=True, input_field_placeholder="Ссылка, запрос для поиска или 00:10-00:40")

TOOLS_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔳 QR-код",        callback_data="tool:qr"),
     InlineKeyboardButton("🎙 Текст в голос", callback_data="tool:tts")],
    [InlineKeyboardButton("🔄 Конвертер",     callback_data="tool:conv"),
     InlineKeyboardButton("💧 Водяной знак",  callback_data="tool:wm")],
    [InlineKeyboardButton("🔗 Моя ссылка",    callback_data="tool:ref")],
])

CONV_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎵 MP3", callback_data="conv:mp3"),
     InlineKeyboardButton("🎼 WAV", callback_data="conv:wav")],
    [InlineKeyboardButton("🎬 MP4", callback_data="conv:mp4"),
     InlineKeyboardButton("🍎 M4A", callback_data="conv:m4a")],
    [InlineKeyboardButton("❌ Отмена", callback_data="conv:cancel")],
])

QUALITY_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 1080p", callback_data="q:1080"),
     InlineKeyboardButton("🎬 720p",  callback_data="q:720")],
    [InlineKeyboardButton("🎬 480p",  callback_data="q:480"),
     InlineKeyboardButton("🎬 360p",  callback_data="q:360")],
    [InlineKeyboardButton("⭐ Лучшее", callback_data="q:best"),
     InlineKeyboardButton("🎵 MP3",    callback_data="q:audio")],
    [InlineKeyboardButton("✂️ Обрезать", callback_data="q:trim"),
     InlineKeyboardButton("📝 Субтитры", callback_data="q:subs")],
    [InlineKeyboardButton("⭐ В закладки", callback_data="q:bk"),
     InlineKeyboardButton("❌ Отмена",     callback_data="q:cancel")],
])

LABELS = {"1080": "1080p", "720": "720p", "480": "480p", "360": "360p",
          "best": "лучшее качество", "audio": "MP3"}

YT_FORMATS = {
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720":  "bv*[height<=720]+ba/b[height<=720]",
    "480":  "bv*[height<=480]+ba/b[height<=480]",
    "360":  "bv*[height<=360]+ba/b[height<=360]",
    "best": "bv*+ba/b",
    "audio": "bestaudio/best",
}

SOCIAL_FORMATS = {
    "1080": "best[height<=1080]/best[height<=1920]/best",
    "720":  "best[height<=720]/best[height<=1280]/best",
    "480":  "best[height<=480]/best",
    "360":  "best[height<=360]/best",
    "best": "best",
    "audio": "bestaudio/best",
}

YT_FALLBACKS = {
    "1080": ["720", "480", "360", "audio"],
    "720":  ["480", "360", "audio"],
    "480":  ["360", "audio"],
    "360":  ["audio"],
    "best": ["720", "480", "360", "audio"],
    "audio": [],
}

SOCIAL_ORDER = ["1080", "720", "480", "360", "best", "audio"]


# ========== Прогресс ==========
def make_bar(p, n=10):
    f = max(0, min(n, int(round(n * p / 100))))
    return "■" * f + "□" * (n - f)


def human_size(b):
    if not b:
        return "?"
    mb = b / (1024 * 1024)
    return f"{mb:.1f} MB" if mb < 100 else f"{mb:.0f} MB"


class ProgressHook:
    def __init__(self, loop, updater, label):
        self.loop, self.updater, self.label = loop, updater, label
        self.last_ts, self.last_p, self.total = 0.0, -1.0, 0

    def _post(self, t):
        try:
            asyncio.run_coroutine_threadsafe(self.updater(t), self.loop)
        except Exception:
            pass

    def __call__(self, d):
        try:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    self.total = total
                p = (d.get("downloaded_bytes") or 0) * 100.0 / self.total if self.total else 0.0
                now = time.time()
                if now - self.last_ts < PROGRESS_EVERY and abs(p - self.last_p) < 5:
                    return
                self.last_ts, self.last_p = now, p
                head = f"🎬 {self.label}" + (f" 💾 {human_size(self.total)}" if self.total else "")
                body = f"📥 Скачиваю:\n{make_bar(p)} {p:.0f}%"
                sp = (d.get("_speed_str") or "").strip()
                if sp and sp != "N/A":
                    body += f"\n⚡ {sp}"
                self._post(f"{head}\n\n{body}")
            elif d.get("status") == "finished":
                head = f"🎬 {self.label}" + (f" 💾 {human_size(self.total)}" if self.total else "")
                self._post(f"{head}\n\n🔄 Обрабатываю файл…")
        except Exception:
            pass


# ========== ffmpeg ==========
def fix_aspect(path):
    if not FIX_ASPECT or path.suffix.lower() not in (".mp4", ".mkv", ".mov"):
        return path
    fixed = path.with_name(path.stem + "_sar.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-c", "copy",
             "-bsf:v", "h264_metadata=sample_aspect_ratio=1/1", str(fixed)],
            capture_output=True, timeout=60, check=True)
        if fixed.exists() and fixed.stat().st_size > 0:
            path.unlink(missing_ok=True)
            return fixed
    except Exception:
        fixed.unlink(missing_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
             "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "23", "-c:a", "copy",
             "-movflags", "+faststart", str(fixed)],
            capture_output=True, timeout=600, check=True)
        if fixed.exists() and fixed.stat().st_size > 0:
            path.unlink(missing_ok=True)
            return fixed
    except Exception:
        log.warning("aspect fix failed", exc_info=True)
        fixed.unlink(missing_ok=True)
    return path


def trim_sync(path, start, end):
    out = path.with_name("trim_" + path.stem + ".mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", start, "-to", end,
         "-i", str(path), "-c", "copy", "-movflags", "+faststart", str(out)],
        capture_output=True, timeout=600, check=True)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("ffmpeg не создал файл")
    return out


def make_qr_sync(text):
    img = qrcode.make(text)
    p = Path(tempfile.gettempdir()) / f"qr_{uuid.uuid4().hex}.png"
    img.save(p)
    return p


def tts_sync(text):
    mp3 = Path(tempfile.gettempdir()) / f"tts_{uuid.uuid4().hex}.mp3"
    ogg = mp3.with_suffix(".ogg")
    try:
        gTTS(text=text, lang="ru").save(str(mp3))
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3),
             "-c:a", "libopus", "-b:a", "64k", str(ogg)],
            capture_output=True, timeout=180, check=True)
        return ogg
    except BaseException:
        ogg.unlink(missing_ok=True)
        raise
    finally:
        mp3.unlink(missing_ok=True)


def convert_sync(src, target):
    out = src.with_name(f"conv_{src.stem}.{target}")
    if target == "mp3":
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn",
               "-c:a", "libmp3lame", "-b:a", "192k", str(out)]
    elif target == "wav":
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn",
               "-c:a", "pcm_s16le", str(out)]
    elif target == "m4a":
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn",
               "-c:a", "aac", "-b:a", "192k", str(out)]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "23", "-c:a", "aac",
               "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, capture_output=True, timeout=900, check=True)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("конвертация не удалась")
    return out


def watermark_sync(src):
    out = src.with_name("wm_" + src.stem + ".mp4")
    vf = (f"drawtext=fontfile={FONT_PATH}:text='{WATERMARK}':"
          "fontcolor=white@0.75:fontsize=h/22:box=1:boxcolor=black@0.35:boxborderw=10:"
          "x=w-tw-24:y=h-th-24")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "copy",
         "-movflags", "+faststart", str(out)],
        capture_output=True, timeout=900, check=True)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("водяной знак не применён")
    return out


# ========== yt-dlp ==========
def base_opts(url):
    p = platform_from_url(url)
    o = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 1,
        "socket_timeout": 15,
        "extractor_retries": 1,
        "file_access_retries": 1,
        "remote_components": ["ejs:github"],
        "extractor_args": {
            "youtube": {
                "player_client": ["web_embedded", "tv_embedded", "default"],
            },
        },
    }
    if COOKIES_FILE and COOKIES_FILE.exists():
        o["cookiefile"] = str(COOKIES_FILE)
    if PROXY_URL and p == "youtube":
        o["proxy"] = PROXY_URL
    return o


def ydl_options(url, kind, tpl, hook=None):
    p = platform_from_url(url)
    o = base_opts(url)
    o["outtmpl"] = tpl
    if hook:
        o["progress_hooks"] = [hook]
    if kind == "audio":
        o["format"] = "bestaudio/best"
        o["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif p == "youtube":
        o["format"] = YT_FORMATS.get(kind, YT_FORMATS["best"])
        o["merge_output_format"] = "mp4"
    else:
        o["format"] = SOCIAL_FORMATS.get(kind, SOCIAL_FORMATS["best"])
        o["format_sort"] = ["res", "br"]
        o["merge_output_format"] = "mp4"
    return o


def extract_info_sync(url):
    o = base_opts(url)
    o["skip_download"] = True
    with yt_dlp.YoutubeDL(o) as y:
        return y.extract_info(url, download=False)


def search_sync(query, n=5):
    o = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": True, "noplaylist": True,
    }
    if COOKIES_FILE and COOKIES_FILE.exists():
        o["cookiefile"] = str(COOKIES_FILE)
    with yt_dlp.YoutubeDL(o) as y:
        info = y.extract_info(f"ytsearch{n}:{query}", download=False)
    return [e for e in (info.get("entries") or []) if e]


def download_sync(url, kind, hook=None):
    p = platform_from_url(url)
    job = DOWNLOAD_DIR / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=True)
    try:
        tpl = str(job / "%(title).90B [%(id)s].%(ext)s")
        with yt_dlp.YoutubeDL(ydl_options(url, kind, tpl, hook)) as y:
            info = y.extract_info(url, download=True)
            expected = Path(y.prepare_filename(info))
        if kind == "audio":
            mp3 = list(job.glob("*.mp3"))
            if mp3:
                return mp3[0]
        res = expected if expected.exists() else None
        if res is None:
            c = [x for x in job.iterdir() if x.is_file()
                 and x.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".ogg", ".opus"}]
            if c:
                res = max(c, key=lambda x: x.stat().st_size)
        if res is None:
            raise FileNotFoundError("файл не найден")
        if p in ("instagram", "facebook", "vk", "tiktok", "twitter", "rutube"):
            res = fix_aspect(res)
        return res
    except BaseException:
        shutil.rmtree(job, ignore_errors=True)
        raise


def download_subs_sync(url):
    job = DOWNLOAD_DIR / ("subs_" + uuid.uuid4().hex)
    job.mkdir(parents=True, exist_ok=True)
    try:
        o = base_opts(url)
        o.update({
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["ru", "en"],
            "subtitlesformat": "srt",
            "outtmpl": str(job / "%(title).90B [%(id)s].%(ext)s"),
        })
        with yt_dlp.YoutubeDL(o) as y:
            y.extract_info(url, download=True)
        files = list(job.glob("*.srt")) + list(job.glob("*.vtt"))
        if not files:
            raise FileNotFoundError("нет субтитров")
        return max(files, key=lambda x: x.stat().st_size)
    except BaseException:
        shutil.rmtree(job, ignore_errors=True)
        raise


def cleanup_file(path):
    if path is None:
        return
    try:
        for x in path.parent.iterdir():
            if x.is_file():
                x.unlink(missing_ok=True)
        path.parent.rmdir()
    except OSError:
        pass


def cleanup_dir(d):
    try:
        for x in Path(d).iterdir():
            if x.is_file():
                x.unlink(missing_ok=True)
        Path(d).rmdir()
    except OSError:
        pass


# ========== Утилиты ==========
def truncate(t, n):
    t = (t or "").strip()
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def fmt_dur(s):
    if not s:
        return ""
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def clean_err(e):
    return re.sub(r"\[\d+(;\d+)*m", "", str(e).replace("\x1b", "")).strip()


def fmt_uptime(s):
    h, r = divmod(s, 3600)
    m, _ = divmod(r, 60)
    return f"{h}ч {m}м"


async def notify_admin(ctx, text):
    if not ADMIN_ID:
        return
    try:
        await ctx.bot.send_message(ADMIN_ID, f"⚠️ {text}")
    except Exception:
        log.warning("admin notify failed", exc_info=True)


def share_keyboard(url, platform, bot_username):
    text = quote(f"🎬 Скачиваю видео в @{bot_username}! → {url}")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Поделиться с другом",
                              url=f"https://t.me/share/url?url=https://t.me/{bot_username}&text={text}")],
        [InlineKeyboardButton("🎬 Скачать ещё", callback_data="share:more")],
    ])


# ========== Очередь ==========
semaphore = asyncio.Semaphore(MAX_CONCURRENT)
stats_lock = asyncio.Lock()
active_jobs = waiting_jobs = completed_jobs = 0
started_at = time.time()


async def acquire_slot():
    global active_jobs, waiting_jobs
    async with stats_lock:
        waiting_jobs += 1
    try:
        await semaphore.acquire()
    except BaseException:
        async with stats_lock:
            waiting_jobs -= 1
        raise
    async with stats_lock:
        waiting_jobs -= 1
        active_jobs += 1


async def release_slot(ok):
    global active_jobs, completed_jobs
    async with stats_lock:
        active_jobs -= 1
        if ok:
            completed_jobs += 1
    semaphore.release()


# ========== Команды ==========
async def cmd_start(update, ctx):
    args = ctx.args or []
    payload = args[0] if args else ""
    u = update.effective_user
    uid = u.id
    stat_user(uid, u.username or "")
    async with _ref_lock:
        me = _refs.setdefault(str(uid), {"invited": 0, "bonus": 0})
        if payload.startswith("ref_"):
            owner = payload[4:]
            if owner.isdigit() and int(owner) != uid and not me.get("referred_by"):
                me["referred_by"] = int(owner)
                od = _refs.setdefault(owner, {"invited": 0, "bonus": 0})
                od["invited"] = od.get("invited", 0) + 1
                if od["invited"] % REF_GOAL == 0:
                    od["bonus"] = od.get("bonus", 0) + 1
        save_refs()

    if not await require_subscription(update, ctx):
        return
    cookie = "есть" if COOKIES_FILE and COOKIES_FILE.exists() else "нет"
    await update.message.reply_text(
        "👋 Привет! Отправь ссылку на видео — пришлю файл.\n\n"
        "▶️ YouTube  📸 Instagram  📘 Facebook\n"
        "🅥 VK  🎵 TikTok  🐦 Twitter/X\n"
        "📺 Rutube  🎧 SoundCloud\n\n"
        "✨ Умею:\n"
        "• 🎬 Скачать видео до 1080p\n"
        "• 🎵 MP3 — только звук\n"
        "• 🔍 Поиск по YouTube\n"
        "• ⭐ Закладки — сохранить на потом\n"
        "• ✂️ Обрезка по времени\n"
        "• 📝 Субтитры\n"
        "• 🛠 QR, текст в голос, конвертер, водяной знак\n"
        f"• 🔗 Рефералка — пригласи {REF_GOAL} друга, получи бонус\n\n"
        f"🍪 Cookies: {cookie}",
        reply_markup=REPLY_MENU)


async def cmd_help(update, ctx):
    if not await require_subscription(update, ctx):
        return
    await update.message.reply_text(
        "🔗 Ссылки:\n"
        "• youtube.com / youtu.be\n"
        "• instagram.com/reel\n"
        "• facebook.com / fb.watch\n"
        "• vk.com / vkvideo.ru\n"
        "• tiktok.com\n"
        "• twitter.com / x.com\n"
        "• rutube.ru/video\n"
        "• soundcloud.com\n\n"
        "⭐ Закладки — команда /saved\n"
        "🔍 Поиск — кнопка «🔍 Поиск» → название\n"
        "✂️ Обрезка — после ссылки «Обрезать» → 00:10-00:40\n"
        "📝 Субтитры — кнопка после ссылки\n"
        "❌ Отмена — кнопка внизу или /cancel\n"
        "💬 Inline — @бот + ссылка в любом чате",
        reply_markup=REPLY_MENU)


async def cmd_status(update, ctx):
    if not await require_subscription(update, ctx):
        return
    async with stats_lock:
        a, w, c = active_jobs, waiting_jobs, completed_jobs
    inv, bonus = ref_stats(update.effective_user.id)
    cookies_note = "✅ загружены" if COOKIES_FILE and COOKIES_FILE.exists() else "❌ нет"
    proxy_note = "✅ вкл" if PROXY_URL else "❌ выкл"
    await update.message.reply_text(
        f"📊 Статус\n"
        f"Активных: {a}/{MAX_CONCURRENT}\n"
        f"В очереди: {w}\n"
        f"Скачано за сессию: {c}\n"
        f"🍪 Cookies: {cookies_note}\n"
        f"🌍 Прокси: {proxy_note}\n"
        f"Приглашено: {inv} (бонусов: {bonus})\n"
        f"Аптайм: {fmt_uptime(int(time.time() - started_at))}",
        reply_markup=REPLY_MENU)


async def cmd_cancel(update, ctx):
    await cancel_user_job(update.effective_user.id)
    reset_user_data(ctx.user_data)
    await update.message.reply_text("Задание отменено. Все состояния сброшены.", reply_markup=REPLY_MENU)


async def cmd_ref(update, ctx):
    if not await require_subscription(update, ctx):
        return
    uid = update.effective_user.id
    inv, bonus = ref_stats(uid)
    await update.message.reply_text(
        f"🔗 Твоя реферальная ссылка:\n{ref_link(ctx.bot.username, uid)}\n\n"
        f"👥 Приглашено: {inv}\n🎁 Бонусов: {bonus}\n\n"
        f"За каждые {REF_GOAL} приглашения — +1 бонус.",
        reply_markup=REPLY_MENU)


async def cmd_saved(update, ctx):
    if not await require_subscription(update, ctx):
        return
    uid = update.effective_user.id
    items = get_bookmarks(uid)
    if not items:
        await update.message.reply_text(
            "⭐ Пока пусто. Открой ссылку и нажми «⭐ В закладки» — потом найдёшь здесь.",
            reply_markup=REPLY_MENU)
        return
    rows = []
    for i, b in enumerate(items[:BOOKMARKS_MAX]):
        plat = PLATFORM_NAMES.get(b.get("platform"), "🎬")
        title = truncate(b.get("title", "Видео"), 45)
        rows.append([InlineKeyboardButton(f"{plat} {title}", callback_data=f"bk:{i}")])
    rows.append([InlineKeyboardButton("❌ Закрыть", callback_data="bk:close")])
    await update.message.reply_text(
        f"⭐ Твои закладки ({len(items)} из {BOOKMARKS_MAX}):",
        reply_markup=InlineKeyboardMarkup(rows))


async def cmd_admin(update, ctx):
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        return
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    total_users = len(_stats["users"])
    dow_today = _stats["downloads"].get(day, 0)
    total_dl = sum(_stats["downloads"].values())
    errors = _stats["errors"]
    tops = sorted(_stats["platforms"].items(), key=lambda x: -x[1])[:5]
    top_str = "\n".join(f"• {PLATFORM_NAMES.get(k, k)} — {v}" for k, v in tops) or "—"
    last7 = []
    for i in range(7):
        d = (now.replace(hour=0, minute=0, second=0, microsecond=0)
             .fromtimestamp(now.timestamp() - i * 86400, tz=timezone.utc))
        ds = d.strftime("%Y-%m-%d")
        last7.append(f"{ds}: {_stats['downloads'].get(ds, 0)}")
    await update.message.reply_text(
        f"👨‍💼 Админ-панель\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"📥 Скачаний сегодня: {dow_today}\n"
        f"📥 Всего: {total_dl}\n"
        f"❌ Ошибок: {errors}\n\n"
        f"🏆 Топ платформ:\n{top_str}\n\n"
        f"📅 Последние 7 дней:\n" + "\n".join(f"• {x}" for x in last7),
        reply_markup=REPLY_MENU)


# ========== Превью + меню ==========
async def show_video_menu(update, ctx, platform, url):
    # No YouTube requests before the quality menu.
    ctx.user_data["title"] = None
    msg = await update.effective_message.reply_text(
        f"{PLATFORM_NAMES.get(platform, platform)}: видео\n\nВыберите качество:",
        reply_markup=QUALITY_MENU)
    menus = ctx.user_data.setdefault("video_menus", {})
    menus[str(msg.message_id)] = (url, platform)
    while len(menus) > 20:
        menus.pop(next(iter(menus)))
async def handle_text(update, ctx):
    if not await require_subscription(update, ctx):
        return
    text = (update.message.text or "").strip()
    state = ctx.user_data.get("await")

    if text == "❌ Отмена":
        await cancel_user_job(update.effective_user.id)
        reset_user_data(ctx.user_data)
        await update.message.reply_text("Отменено.", reply_markup=REPLY_MENU)
        return
    if text == "🎬 Скачать видео":
        ctx.user_data.pop("await", None)
        await update.message.reply_text("Пришли ссылку на видео.", reply_markup=REPLY_MENU)
        return
    if text == "🎵 MP3":
        ctx.user_data.pop("await", None)
        await update.message.reply_text("Пришли ссылку; затем выбери MP3.", reply_markup=REPLY_MENU)
        return
    if text == "🛠 Инструменты":
        ctx.user_data.pop("await", None)
        await update.message.reply_text("🛠 Выбери инструмент:", reply_markup=TOOLS_MENU)
        return
    if text == "🔍 Поиск":
        ctx.user_data["await"] = "search"
        await update.message.reply_text("🔍 Что искать на YouTube? Напиши название.", reply_markup=REPLY_MENU)
        return
    if text == "⭐ Закладки":
        await cmd_saved(update, ctx)
        return
    if text == "ℹ️ Помощь":
        ctx.user_data.pop("await", None)
        await cmd_help(update, ctx)
        return
    if text == "📊 Статус":
        ctx.user_data.pop("await", None)
        await cmd_status(update, ctx)
        return
    if text == "🔗 Моя ссылка":
        ctx.user_data.pop("await", None)
        await cmd_ref(update, ctx)
        return

    platform, url = detect_platform(text)
    if platform:
        reset_user_data(ctx.user_data)
        ctx.user_data["url"] = url
        ctx.user_data["platform"] = platform
        await show_video_menu(update, ctx, platform, url)
        return

    if state == "qr":
        ctx.user_data.pop("await", None)
        if len(text) > 1000:
            await update.message.reply_text("Слишком длинный текст для QR.", reply_markup=REPLY_MENU)
            return
        msg = await update.message.reply_text("🔳 Создаю QR-код…")
        loop = asyncio.get_running_loop()
        p = None
        try:
            p = await loop.run_in_executor(None, make_qr_sync, text)
            with p.open("rb") as f:
                await update.message.reply_photo(photo=f, caption=f"🔳 QR-код: {truncate(text, 60)}")
            await msg.delete()
        except Exception:
            log.warning("qr failed", exc_info=True)
            await msg.edit_text("❌ Не удалось создать QR-код.")
        finally:
            if p:
                p.unlink(missing_ok=True)
        await update.message.reply_text("Что дальше?", reply_markup=REPLY_MENU)
        return

    if state == "tts":
        ctx.user_data.pop("await", None)
        if len(text) > 800:
            await update.message.reply_text("Слишком длинный текст (макс. 800).", reply_markup=REPLY_MENU)
            return
        msg = await update.message.reply_text("🎙 Озвучиваю…")
        loop = asyncio.get_running_loop()
        p = None
        try:
            p = await loop.run_in_executor(None, tts_sync, text)
            with p.open("rb") as f:
                await ctx.bot.send_voice(chat_id=update.effective_chat.id, voice=f, caption="🎙 Готово!")
            await msg.delete()
        except Exception:
            log.warning("tts failed", exc_info=True)
            await msg.edit_text("❌ Не удалось озвучить текст.")
        finally:
            if p:
                p.unlink(missing_ok=True)
        await update.message.reply_text("Что дальше?", reply_markup=REPLY_MENU)
        return

    if state == "search":
        ctx.user_data.pop("await", None)
        await do_search(update, ctx, text)
        return

    if state == "trim":
        url = ctx.user_data.get("trim_url")
        m = re.match(
            r"^\s*(\d{1,2}:\d{1,2}(?::\d{1,2})?)\s*[-–—~]\s*(\d{1,2}:\d{1,2}(?::\d{1,2})?)\s*$",
            text)
        if not m or not url:
            ctx.user_data.pop("await", None)
            ctx.user_data.pop("trim_url", None)
            await update.message.reply_text(
                "Не похоже на диапазон (пример: 00:10-00:40). Действие отменено.",
                reply_markup=REPLY_MENU)
            return
        ctx.user_data.pop("await", None)
        ctx.user_data.pop("trim_url", None)
        await download_and_send(update, ctx, url, "best", trim=(m.group(1), m.group(2)))
        return

    if state in ("conv_file", "wm_file"):
        ctx.user_data.pop("await", None)
        await update.message.reply_text(
            "Пришлите файл документом. Можно снова нажать «🛠 Инструменты».",
            reply_markup=REPLY_MENU)
        return

    await do_search(update, ctx, text)


# ========== Документы ==========
async def on_document(update, ctx):
    if not await require_subscription(update, ctx):
        return
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Пришли файл документом.", reply_markup=REPLY_MENU)
        return
    state = ctx.user_data.get("await")
    if state not in ("conv_file", "wm_file"):
        await update.message.reply_text(
            "Пришли ссылку на видео или выбери «🛠 Инструменты».", reply_markup=REPLY_MENU)
        return
    if (doc.file_size or 0) > MAX_UPLOAD_MB * 1024 * 1024:
        await update.message.reply_text(
            f"❌ Файл больше {MAX_UPLOAD_MB} МБ.", reply_markup=REPLY_MENU)
        return

    msg = await update.message.reply_text("📥 Скачиваю файл…")
    job = DOWNLOAD_DIR / ("doc_" + uuid.uuid4().hex)
    job.mkdir(parents=True, exist_ok=True)
    # Never use a user-supplied path as a filesystem path.
    filename = Path((doc.file_name or "upload.bin").replace("\\", "/")).name
    src = job / ("input_" + (filename or "upload.bin"))
    try:
        f = await doc.get_file()
        await f.download_to_drive(str(src))
    except Exception:
        log.warning("download doc failed", exc_info=True)
        await msg.edit_text("❌ Не удалось скачать файл из Telegram.")
        cleanup_dir(job)
        return

    if state == "conv_file":
        cleanup_pending_conversion(ctx.user_data)
        ctx.user_data["conv_src"] = str(src)
        ctx.user_data.pop("await", None)
        await msg.edit_text("🔄 В какой формат конвертировать?", reply_markup=CONV_MENU)
        return

    ctx.user_data.pop("await", None)
    await msg.edit_text("💧 Накладываю водяной знак…")
    loop = asyncio.get_running_loop()
    out = None
    try:
        out = await loop.run_in_executor(None, watermark_sync, src)
        if out.stat().st_size / (1024 * 1024) > MAX_SIZE_MB:
            await ctx.bot.send_message(
                update.effective_chat.id,
                f"❌ После обработки больше {MAX_SIZE_MB} МБ.")
        else:
            with out.open("rb") as fh:
                await ctx.bot.send_video(chat_id=update.effective_chat.id, video=fh,
                                         caption=f"💧 Водяной знак: {WATERMARK}")
        await msg.delete()
    except Exception:
        log.warning("watermark failed", exc_info=True)
        await msg.edit_text("❌ Не удалось наложить водяной знак.")
    finally:
        if out:
            out.unlink(missing_ok=True)
        cleanup_dir(job)
    await update.message.reply_text("Что дальше?", reply_markup=REPLY_MENU)


# ========== Поиск ==========
async def do_search(update, ctx, query):
    if not query or len(query) < 2:
        await update.effective_message.reply_text("Слишком короткий запрос.", reply_markup=REPLY_MENU)
        return
    msg = await update.effective_message.reply_text("🔍 Ищу на YouTube…")
    loop = asyncio.get_running_loop()
    try:
        results = await asyncio.wait_for(
            loop.run_in_executor(None, search_sync, query, 5), timeout=40)
    except Exception:
        log.warning("search failed", exc_info=True)
        await msg.edit_text("Не удалось выполнить поиск.")
        return
    if not results:
        await msg.edit_text("Ничего не нашёл.")
        return
    await msg.delete()
    ctx.user_data["search_results"] = []
    rows = []
    for i, e in enumerate(results[:5]):
        title = truncate(e.get("title") or "Видео", 60)
        vid = e.get("id")
        url = f"https://www.youtube.com/watch?v={vid}" if vid else e.get("url")
        if not url:
            continue
        ctx.user_data["search_results"].append(url)
        idx = len(ctx.user_data["search_results"]) - 1
        rows.append([InlineKeyboardButton(f"{i+1}. {title[:55]}", callback_data=f"s:{idx}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="s:cancel")])
    await update.effective_message.reply_text(
        "🔍 Нашёл на YouTube:\nНажми, чтобы скачать.",
        reply_markup=InlineKeyboardMarkup(rows))


# ========== Скачивание ==========
async def _download_and_send(update, ctx, url, requested_kind, via_callback=False, trim=None):
    query = update.callback_query if via_callback else None
    message = query.message if query else update.effective_message
    chat_id = message.chat_id
    user_id = update.effective_user.id
    platform = platform_from_url(url)

    if platform == "youtube":
        attempts = [requested_kind] + YT_FALLBACKS.get(requested_kind, [])
    elif requested_kind == "audio":
        attempts = ["audio"]
    else:
        attempts = (SOCIAL_ORDER[SOCIAL_ORDER.index(requested_kind):]
                    if requested_kind in SOCIAL_ORDER else list(SOCIAL_ORDER))

    await acquire_slot()
    ok = False
    loop = asyncio.get_running_loop()
    cookies_alerted = False

    try:
        for idx, kind in enumerate(attempts):
            label = LABELS.get(kind, kind)
            head = f"🎬 {label}\n\n📥 Готовлю загрузку…"
            if idx:
                head = f"🎬 {label}\n\n⏳ Пробую снова…"

            if via_callback:
                try:
                    await query.edit_message_text(head)
                    pmsg = query.message
                except Exception:
                    pmsg = await ctx.bot.send_message(chat_id, head)
            else:
                pmsg = await message.reply_text(head)

            async def upd(t, _m=pmsg):
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=_m.chat_id, message_id=_m.message_id, text=t)
                except Exception:
                    pass

            hook = ProgressHook(loop, upd, label)
            await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
            path = None

            try:
                path = await run_media_worker("download", url, kind, hook)

                if trim:
                    try:
                        trimmed = await loop.run_in_executor(
                            None, trim_sync, path, trim[0], trim[1])
                        # Source and trimmed output are in the same directory.
                        path.unlink(missing_ok=True)
                        path = trimmed
                    except Exception:
                        log.warning("trim failed", exc_info=True)
                        await ctx.bot.send_message(
                            chat_id, "Не удалось обрезать. Отправляю полное.")

                if path.stat().st_size / (1024 * 1024) > MAX_SIZE_MB:
                    cleanup_file(path)
                    path = None
                    continue

                try:
                    await ctx.bot.edit_message_text(
                        chat_id=pmsg.chat_id, message_id=pmsg.message_id,
                        text=f"🎬 {label} 💾 {human_size(path.stat().st_size)}\n\n📤 Загружаю…")
                except Exception:
                    pass

                with path.open("rb") as fh:
                    if kind == "audio":
                        await ctx.bot.send_audio(chat_id=chat_id, audio=fh,
                                                 title=path.stem, caption="🎵 Готово!")
                    else:
                        await ctx.bot.send_video(chat_id=chat_id, video=fh,
                                                 supports_streaming=True,
                                                 caption=f"🎬 Готово! ({label})")

                ok = True
                stat_download(platform)
                await add_bookmark(user_id, url, ctx.user_data.get("title"), platform)

                try:
                    await ctx.bot.delete_message(pmsg.chat_id, pmsg.message_id)
                except Exception:
                    pass

                await ctx.bot.send_message(
                    chat_id,
                    "✅ Готово! Сохранил в ⭐ Закладки (команда /saved)",
                    reply_markup=share_keyboard(url, platform, ctx.bot.username or "bot"))
                return

            except yt_dlp.utils.DownloadError as e:
                err = str(e)
                log.warning("format %s: %s", kind, clean_err(e))
                _stats["errors"] = _stats.get("errors", 0) + 1
                if not cookies_alerted and ("Sign in to confirm" in err or "cookies" in err.lower()):
                    cookies_alerted = True
                    await notify_admin(ctx, "Cookies YouTube устарели — обновите Secret Files.")
                try:
                    await ctx.bot.delete_message(pmsg.chat_id, pmsg.message_id)
                except Exception:
                    pass
                if "requested format is not available" not in err.lower() or idx == len(attempts) - 1:
                    raise
            finally:
                cleanup_file(path)

        await ctx.bot.send_message(chat_id, unavailable_message(), reply_markup=REPLY_MENU)

    except yt_dlp.utils.DownloadError as e:
        log.warning("download failed: %s", clean_err(e))
        _stats["errors"] = _stats.get("errors", 0) + 1
        await ctx.bot.send_message(chat_id, unavailable_message(), reply_markup=REPLY_MENU)
    except Exception:
        log.exception("download error")
        _stats["errors"] = _stats.get("errors", 0) + 1
        await ctx.bot.send_message(chat_id, unavailable_message(), reply_markup=REPLY_MENU)
    finally:
        await release_slot(ok)


# ========== Субтитры ==========
async def _send_subs(update, ctx, url):
    q = update.callback_query
    try:
        await q.edit_message_text("📝 Ищу субтитры…")
    except Exception:
        pass
    loop = asyncio.get_running_loop()
    path = None
    try:
        path = await run_media_worker("subs", url)
        with path.open("rb") as f:
            await ctx.bot.send_document(chat_id=q.message.chat_id, document=f,
                                        filename=path.name, caption="📝 Субтитры")
        try:
            await q.message.delete()
        except Exception:
            pass
        await ctx.bot.send_message(q.message.chat_id, "Что дальше?", reply_markup=REPLY_MENU)
    except Exception:
        log.warning("subs failed", exc_info=True)
        try:
            await q.edit_message_text("❌ Субтитры не найдены.")
        except Exception:
            pass
    finally:
        cleanup_file(path)


# ========== Кнопки ==========
async def on_callback(update, ctx):
    q = update.callback_query
    data = q.data or ""

    if data == "sub:check":
        if await check_subscription(ctx.bot, q.from_user.id):
            await q.answer("Подписка подтверждена")
            await q.message.reply_text("Спасибо! Теперь отправь ссылку.", reply_markup=REPLY_MENU)
        else:
            await q.answer("Сначала подпишись на канал.", show_alert=True)
        return

    if data == "share:more":
        await q.answer("Пришли новую ссылку!")
        return

    await q.answer()

    if data.startswith("bk:"):
        val = data[3:]
        if val == "close":
            try:
                await q.message.delete()
            except Exception:
                pass
            return
        items = get_bookmarks(q.from_user.id)
        if not val.isdigit() or int(val) >= len(items):
            try:
                await q.edit_message_text("Закладка устарела.")
            except Exception:
                pass
            return
        b = items[int(val)]
        reset_user_data(ctx.user_data)
        ctx.user_data["url"] = b["url"]
        ctx.user_data["platform"] = b.get("platform", "other")
        try:
            await q.message.delete()
        except Exception:
            pass
        chat_id = q.message.chat_id
        await show_video_menu(update, ctx, b.get("platform", "other"), b["url"])
        return

    if data.startswith("tool:"):
        tool = data[5:]
        if tool == "qr":
            ctx.user_data["await"] = "qr"
            await q.edit_message_text("🔳 Пришли текст или ссылку.")
        elif tool == "tts":
            ctx.user_data["await"] = "tts"
            await q.edit_message_text("🎙 Пришли текст (до 800 символов).")
        elif tool == "conv":
            ctx.user_data["await"] = "conv_file"
            await q.edit_message_text(
                f"🔄 Пришли файл документом.\nЛимит: {MAX_UPLOAD_MB} МБ.")
        elif tool == "wm":
            ctx.user_data["await"] = "wm_file"
            await q.edit_message_text(
                f"💧 Пришли видео документом — наложу «{WATERMARK}».\nЛимит: {MAX_UPLOAD_MB} МБ.")
        elif tool == "ref":
            uid = q.from_user.id
            inv, bonus = ref_stats(uid)
            await q.edit_message_text(
                f"🔗 Твоя ссылка:\n{ref_link(ctx.bot.username, uid)}\n\n"
                f"👥 Приглашено: {inv}\n🎁 Бонусов: {bonus}")
        return

    if data.startswith("conv:"):
        target = data[5:]
        if target == "cancel":
            cleanup_pending_conversion(ctx.user_data)
            try:
                await q.edit_message_text("❌ Отменено.")
            except Exception:
                pass
            return
        if target not in {"mp3", "wav", "mp4", "m4a"}:
            return
        src_path = ctx.user_data.pop("conv_src", None)
        if not src_path or not Path(src_path).exists():
            try:
                await q.edit_message_text("Файл устарел.")
            except Exception:
                pass
            return
        try:
            await q.edit_message_text(f"🔄 Конвертирую в {target.upper()}…")
        except Exception:
            pass
        loop = asyncio.get_running_loop()
        out = None
        try:
            out = await loop.run_in_executor(None, convert_sync, Path(src_path), target)
            size_mb = out.stat().st_size / (1024 * 1024)
            if size_mb > MAX_SIZE_MB:
                await ctx.bot.send_message(
                    q.message.chat_id, f"❌ Результат {size_mb:.1f} МБ > {MAX_SIZE_MB}")
            else:
                with out.open("rb") as f:
                    if target in ("mp3", "wav", "m4a"):
                        await ctx.bot.send_audio(chat_id=q.message.chat_id, audio=f,
                                                 title=out.stem,
                                                 caption=f"🔄 Готово ({target.upper()})")
                    else:
                        await ctx.bot.send_video(chat_id=q.message.chat_id, video=f,
                                                 caption=f"🔄 Готово ({target.upper()})")
            try:
                await q.message.delete()
            except Exception:
                pass
            await ctx.bot.send_message(q.message.chat_id, "Что дальше?", reply_markup=REPLY_MENU)
        except Exception:
            log.warning("convert failed", exc_info=True)
            await ctx.bot.send_message(q.message.chat_id, "❌ Не удалось конвертировать.")
        finally:
            if out:
                out.unlink(missing_ok=True)
            cleanup_dir(Path(src_path).parent)
        return

    if data.startswith("s:"):
        val = data[2:]
        if val == "cancel":
            try:
                await q.edit_message_text("Поиск отменён.")
            except Exception:
                pass
            return
        results = ctx.user_data.get("search_results") or []
        if not val.isdigit() or int(val) >= len(results):
            try:
                await q.edit_message_text("Результат устарел.")
            except Exception:
                pass
            return
        await download_and_send(update, ctx, results[int(val)], "best", via_callback=True)
        return

    action = data.removeprefix("q:")
    if data.startswith("q:") and action != "cancel":
        bound = ctx.user_data.get("video_menus", {}).get(str(q.message.message_id))
        if not bound:
            await q.message.reply_text("Это меню устарело. Отправьте ссылку заново.")
            return
        ctx.user_data["url"], ctx.user_data["platform"] = bound


    if action == "cancel":
        await cancel_user_job(update.effective_user.id)
        reset_user_data(ctx.user_data)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        try:
            await q.edit_message_text("❌ Отменено.")
        except Exception:
            pass
        return

    if not await check_subscription(ctx.bot, q.from_user.id):
        await q.message.reply_text(f"Подпишись на {CHANNEL}.", reply_markup=sub_keyboard())
        return

    url = ctx.user_data.get("url")
    if not url:
        try:
            await q.edit_message_text("Ссылка устарела.")
        except Exception:
            pass
        return

    if action == "subs":
        await send_subs(update, ctx, url)
        return

    if action == "bk":
        await add_bookmark(q.from_user.id, url, ctx.user_data.get("title"),
                           ctx.user_data.get("platform", "other"))
        await q.answer("⭐ Добавлено в закладки!", show_alert=True)
        return

    if action == "trim":
        ctx.user_data["await"] = "trim"
        ctx.user_data["trim_url"] = url
        try:
            await q.edit_message_text("✂️ Пришли диапазон, например: 00:10-00:40")
        except Exception:
            pass
        return

    await download_and_send(update, ctx, url, action, via_callback=True)


# ========== Inline ==========
async def on_inline(update, ctx):
    q = update.inline_query
    query = (q.query or "").strip()
    platform, url = detect_platform(query)
    bot_uname = ctx.bot.username or ""
    if not url:
        results = [InlineQueryResultArticle(
            id="hint", title="🎬 Пришлите ссылку",
            description="YouTube, Instagram, VK, TikTok, Rutube, SoundCloud",
            input_message_content=InputTextMessageContent(
                f"🎬 Скачиваю видео в @{bot_uname}"))]
        await q.answer(results, cache_time=5)
        return
    results = [InlineQueryResultArticle(
        id="dl", title=f"🎬 Скачать видео ({platform})",
        description="Открой бота и пришли ссылку",
        input_message_content=InputTextMessageContent(
            f"🎬 Скачать это видео:\n{url}\n\n👉 @{bot_uname}"))]
    await q.answer(results, cache_time=5)


# ========== Health ==========
def start_health_server():
    port = int(os.getenv("PORT", "0"))
    if not port:
        return

    class H(BaseHTTPRequestHandler):
        def _ok(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(b"OK - bot is running")))
            self.end_headers()
            if body:
                self.wfile.write(b"OK - bot is running")

        def do_GET(self):
            self._ok(True)

        def do_HEAD(self):
            self._ok(False)

        def log_message(self, *a):
            pass

    HTTPServer(("0.0.0.0", port), H).serve_forever()


# ========== Дайджест ==========
async def digest_loop(app):
    while True:
        try:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            await asyncio.sleep(wait)
            if not ADMIN_ID:
                continue
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            dl = _stats["downloads"].get(day, 0)
            total_dl = sum(_stats["downloads"].values())
            tops = sorted(_stats["platforms"].items(), key=lambda x: -x[1])[:3]
            top_str = ", ".join(f"{PLATFORM_NAMES.get(k, k)} ({v})" for k, v in tops) or "—"
            async with stats_lock:
                a, w, c = active_jobs, waiting_jobs, completed_jobs
            await app.bot.send_message(
                ADMIN_ID,
                f"📊 Дайджест за {day}\n\n"
                f"📥 Скачано сегодня: {dl}\n"
                f"📥 Всего: {total_dl}\n"
                f"👥 Пользователей: {len(_stats['users'])}\n"
                f"❌ Ошибок: {_stats['errors']}\n"
                f"🏆 Топ: {top_str}\n"
                f"⚙️ Сейчас: активно {a}, в очереди {w}")
        except Exception:
            log.warning("digest loop error", exc_info=True)
            await asyncio.sleep(3600)


# ========== Запуск ==========
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "🏠 Меню"),
        BotCommand("saved", "⭐ Закладки"),
        BotCommand("help", "ℹ️ Помощь"),
        BotCommand("ref", "🔗 Моя ссылка"),
        BotCommand("status", "📊 Статус"),
        BotCommand("cancel", "❌ Отмена"),
    ])
    app.bot_data["digest_task"] = asyncio.create_task(digest_loop(app), name="digest")


async def post_stop(app: Application):
    await stop_all_jobs()
    task = app.bot_data.pop("digest_task", None)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    for user_data in app.user_data.values():
        cleanup_pending_conversion(user_data)
    save_refs()
    save_bookmarks()


async def on_error(update, ctx):
    # Do not print exception text: upstream URLs may contain credentials.
    log.error("Unhandled handler error: %s", type(ctx.error).__name__)


def cleanup_pending_conversion(user_data):
    src = user_data.pop("conv_src", None)
    if src:
        parent = Path(src).parent
        if parent.parent.resolve() == DOWNLOAD_DIR.resolve() and parent.name.startswith("doc_"):
            shutil.rmtree(parent, ignore_errors=True)


def reset_user_data(user_data):
    cleanup_pending_conversion(user_data)
    user_data.clear()


def main():
    if not TOKEN:
        raise SystemExit("Не задана BOT_TOKEN.")
    load_refs()
    load_bookmarks()
    if os.getenv("PORT"):
        Thread(target=start_health_server, daemon=True).start()

    app = (Application.builder()
           .token(TOKEN)
           .post_init(post_init)
           .post_stop(post_stop)
           .connect_timeout(30).read_timeout(120).write_timeout(120).pool_timeout(30)
           .media_write_timeout(120)
           .get_updates_connect_timeout(30).get_updates_read_timeout(30)
           .build())

    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("ref", cmd_ref))
    app.add_handler(CommandHandler("saved", cmd_saved))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(q:|sub:|s:|tool:|conv:|bk:|share:)"))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Бот запускается. Прокси: %s", bool(PROXY_URL))
    app.run_polling(allowed_updates=Update.ALL_TYPES)



# Background jobs are deliberately separate from the sequential update handler.
# Each job receives a snapshot; later links cannot change its URL/title.
_user_jobs = {}

async def cancel_user_job(uid):
    task = _user_jobs.get(uid)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

async def stop_all_jobs():
    tasks = list(_user_jobs.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _user_jobs.clear()

async def launch_job(update, ctx, operation, *args, **kwargs):
    uid = update.effective_user.id
    if uid in _user_jobs:
        await update.effective_message.reply_text(
            "У вас уже есть задание. Дождитесь окончания или отправьте /cancel.")
        return
    if len(_user_jobs) >= QUEUE_LIMIT + MAX_CONCURRENT:
        await update.effective_message.reply_text("Очередь заполнена. Попробуйте позже.")
        return
    snapshot = SimpleNamespace(bot=ctx.bot, user_data=dict(ctx.user_data), application=ctx.application)
    await update.effective_message.reply_text(
        f"Задание принято. Лимит ожидания и обработки: {JOB_TIMEOUT} сек. Отмена: /cancel")
    async def run():
        try:
            await asyncio.wait_for(operation(update, snapshot, *args, **kwargs), JOB_TIMEOUT)
        except asyncio.TimeoutError:
            await update.effective_message.reply_text(
                "Превышено время задания. Загрузка остановлена. Попробуйте короткое видео или меньшее качество.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Background job failed: %s", type(exc).__name__)
            await update.effective_message.reply_text("Не удалось выполнить задание. Попробуйте позже.")
        finally:
            _user_jobs.pop(uid, None)
    _user_jobs[uid] = asyncio.create_task(run())

async def download_and_send(update, ctx, *args, **kwargs):
    await launch_job(update, ctx, _download_and_send, *args, **kwargs)

async def send_subs(update, ctx, url):
    # Subtitles also share the global concurrency limit.
    async def operation(u, c):
        await acquire_slot()
        try:
            await _send_subs(u, c, url)
        finally:
            await release_slot(False)
    await launch_job(update, ctx, operation)

async def run_media_worker(mode, url, kind="best", hook=None):
    # Unlike wait_for(executor), killing this subprocess really stops yt-dlp
    # and its ffmpeg children before temporary files are removed (Linux/Render).
    job = Path(tempfile.mkdtemp(prefix="worker_", dir=DOWNLOAD_DIR))
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve()), "--media-worker", str(job),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        proc.stdin.write(json.dumps({"mode": mode, "url": url, "kind": kind}).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        result = None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if event.get("event") == "progress" and hook:
                hook(event["data"])
            elif event.get("event") == "result":
                result = event
        await proc.wait()
        if not result or not result.get("ok"):
            raise yt_dlp.utils.DownloadError((result or {}).get("error", "Загрузчик завершился без результата"))
        path = Path(result["path"])
        if job.resolve() not in path.resolve().parents or not path.is_file():
            raise RuntimeError("Invalid worker output")
        # Move the complete media directory out of the process workspace.
        target = DOWNLOAD_DIR / uuid.uuid4().hex
        shutil.move(str(path.parent), str(target))
        return target / path.name
    finally:
        if proc:
            # Kill the entire session even if the direct child already exited.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        shutil.rmtree(job, ignore_errors=True)

def media_worker():
    global DOWNLOAD_DIR
    DOWNLOAD_DIR = Path(sys.argv[2])
    payload = json.load(sys.stdin)
    last = [0.0]
    def emit(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)
    def progress(data):
        now = time.monotonic()
        if now - last[0] < PROGRESS_EVERY:
            return
        last[0] = now
        fields = ("status", "downloaded_bytes", "total_bytes", "total_bytes_estimate", "speed", "eta")
        emit({"event": "progress", "data": {k: data.get(k) for k in fields}})
    try:
        path = (download_subs_sync(payload["url"]) if payload["mode"] == "subs"
                else download_sync(payload["url"], payload["kind"], progress))
        emit({"event": "result", "ok": True, "path": str(path)})
    except Exception as exc:
        emit({"event": "result", "ok": False, "error": clean_err(exc)[:1500]})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--media-worker":
        media_worker()
    else:
        main()
