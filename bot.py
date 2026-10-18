# bot.py — YouTube/Instagram/Facebook/VK/TikTok/Twitter + поиск + inline + обрезка + субтитры + реф
import os
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
from urllib.parse import urlsplit
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

import yt_dlp
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

TOKEN          = os.getenv("BOT_TOKEN")
MAX_SIZE_MB    = int(os.getenv("MAX_SIZE_MB", "48"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
PROXY_URL      = os.getenv("PROXY_URL", "").strip()
PROGRESS_EVERY = float(os.getenv("PROGRESS_EVERY", "3"))
FIX_ASPECT     = os.getenv("FIX_ASPECT", "0") == "1"
REF_GOAL       = int(os.getenv("REF_GOAL", "3"))  # приглашений для бонуса

CHANNEL       = "@edrikevideo"
CHANNEL_URL   = "https://t.me/edrikevideo"
CONTACT_TEXT  = "напиши @edrikevideo"

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdl_bot"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
REF_FILE = Path(tempfile.gettempdir()) / "ytdl_refs.json"

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
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
        log.exception("cookies copy failed")
elif LOCAL_COOKIES.exists():
    COOKIES_FILE = LOCAL_COOKIES
log.info("Cookies: %s | Прокси: %s | FIX_ASPECT: %s", bool(COOKIES_FILE), bool(PROXY_URL), FIX_ASPECT)

# ---------- Рефералка ----------
_refs = {}
_ref_lock = asyncio.Lock()

def load_refs():
    global _refs
    try:
        if REF_FILE.exists():
            _refs = json.loads(REF_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.warning("refs load failed", exc_info=True)
        _refs = {}

def save_refs():
    try:
        REF_FILE.write_text(json.dumps(_refs, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.warning("refs save failed", exc_info=True)

def ref_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"

def ref_stats(user_id: int):
    d = _refs.get(str(user_id), {})
    return d.get("invited", 0), d.get("bonus", 0)

# ---------- Платформы ----------
PLATFORM_PATTERNS = [
    ("youtube",   re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/[\w\-?=&/%.]+", re.I)),
    ("instagram", re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv|share)/[\w\-?=&/%.]+", re.I)),
    ("facebook",  re.compile(r"https?://(?:(?:www|m|web|ru-ru)\.)?(?:facebook\.com|fb\.watch|fb\.com)/[\w\-?=&/%.]+", re.I)),
    ("vk",        re.compile(r"https?://(?:(?:m|new)\.)?(?:vk\.com|vkvideo\.ru|vk\.ru)/[\w\-?=&/%.]+", re.I)),
    ("tiktok",    re.compile(r"https?://(?:www\.|m\.)?(?:tiktok\.com|vt\.tiktok\.com)/[\w\-?=&/%.@]+", re.I)),
    ("twitter",   re.compile(r"https?://(?:(?:www\.|mobile\.)?(?:twitter\.com|x\.com))/[\w\-?=&/%.]+", re.I)),
]
PLATFORM_NAMES = {
    "youtube": "▶️ YouTube", "instagram": "📸 Instagram", "facebook": "📘 Facebook",
    "vk": "🅥 VK", "tiktok": "🎵 TikTok", "twitter": "🐦 Twitter / X",
}

def unavailable_message() -> str:
    return ("🛑 Ошибка скачивания!\nЭтот видеоролик недоступен.\n"
            f"Попробуй повторить запрос.\nЕсли видео не загрузилось, {CONTACT_TEXT}.")

def detect_platform(text: str):
    for p, rx in PLATFORM_PATTERNS:
        m = rx.search(text or "")
        if m:
            return p, m.group(0).rstrip(".,)")
    return None, None

def platform_from_url(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    if h in ("youtu.be", "youtube.com") or h.endswith(".youtube.com"): return "youtube"
    if h == "instagram.com" or h.endswith(".instagram.com"): return "instagram"
    if h == "fb.watch" or h.endswith(".fb.watch") or h.endswith("facebook.com"): return "facebook"
    if h.endswith("vk.com") or h.endswith("vk.ru") or h.endswith("vkvideo.ru"): return "vk"
    if h.endswith("tiktok.com"): return "tiktok"
    if h.endswith("twitter.com") or h.endswith("x.com"): return "twitter"
    return "other"

def fetch_thumb_bytes(url: str, platform: str):
    if not url: return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        if PROXY_URL and platform == "youtube" and PROXY_URL.startswith("http"):
            op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}))
        else:
            op = urllib.request.build_opener()
        with op.open(req, timeout=15) as r:
            data = r.read()
        return data or None
    except Exception:
        log.warning("thumb failed: %s", url, exc_info=True)
        return None

# ---------- Подписка ----------
async def check_subscription(bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL, user_id)
        return m.status not in ("left", "kicked")
    except Exception:
        log.exception("sub check failed")
        return False

def sub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton("✅ Я подписался", callback_data="sub:check")],
    ])

async def require_subscription(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if await check_subscription(ctx.bot, update.effective_user.id):
        return True
    await update.effective_message.reply_text(
        f"Чтобы пользоваться ботом, подпишитесь на {CHANNEL} и нажмите «✅ Я подписался».",
        reply_markup=sub_keyboard())
    return False

# ---------- Меню ----------
REPLY_MENU = ReplyKeyboardMarkup([
    [KeyboardButton("🎬 Скачать видео"), KeyboardButton("🔍 Поиск")],
    [KeyboardButton("🎵 MP3"), KeyboardButton("🔗 Моя ссылка")],
    [KeyboardButton("ℹ️ Помощь"), KeyboardButton("📊 Статус")],
], resize_keyboard=True, input_field_placeholder="Ссылка, запрос для поиска или 00:10-00:40")

QUALITY_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 1080p", callback_data="q:1080"),
     InlineKeyboardButton("🎬 720p",  callback_data="q:720")],
    [InlineKeyboardButton("🎬 480p",  callback_data="q:480"),
     InlineKeyboardButton("🎬 360p",  callback_data="q:360")],
    [InlineKeyboardButton("⭐ Лучшее", callback_data="q:best"),
     InlineKeyboardButton("🎵 MP3",    callback_data="q:audio")],
    [InlineKeyboardButton("✂️ Обрезать", callback_data="q:trim"),
     InlineKeyboardButton("📝 Субтитры", callback_data="q:subs")],
    [InlineKeyboardButton("❌ Отмена", callback_data="q:cancel")],
])

LABELS = {"1080": "1080p", "720": "720p", "480": "480p", "360": "360p",
          "best": "лучшее качество", "audio": "MP3"}

YT_FORMATS = {
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720":  "bv*[height<=720]+ba/b[height<=720]",
    "480":  "bv*[height<=480]+ba/b[height<=480]",
    "360":  "bv*[height<=360]+ba/b[height<=360]",
    "best": "bv*+ba/b", "audio": "bestaudio/best",
}
SOCIAL_FORMATS = {
    "1080": "best[height<=1080]/best[height<=1920]/best",
    "720":  "best[height<=720]/best[height<=1280]/best",
    "480":  "best[height<=480]/best", "360": "best[height<=360]/best",
    "best": "best", "audio": "bestaudio/best",
}
YT_FALLBACKS = {
    "1080": ["720", "480", "360", "audio"], "720": ["480", "360", "audio"],
    "480": ["360", "audio"], "360": ["audio"],
    "best": ["720", "480", "360", "audio"], "audio": [],
}
SOCIAL_ORDER = ["1080", "720", "480", "360", "best", "audio"]

# ---------- Прогресс ----------
def make_bar(p: float, n: int = 10) -> str:
    f = max(0, min(n, int(round(n * p / 100))))
    return "■" * f + "□" * (n - f)

def human_size(b) -> str:
    if not b: return "?"
    mb = b / (1024 * 1024)
    return f"{mb:.1f} MB" if mb < 100 else f"{mb:.0f} MB"

class ProgressHook:
    def __init__(self, loop, updater, label):
        self.loop, self.updater, self.label = loop, updater, label
        self.last_ts, self.last_p, self.total = 0.0, -1.0, 0
    def _post(self, t):
        try: asyncio.run_coroutine_threadsafe(self.updater(t), self.loop)
        except Exception: pass
    def __call__(self, d):
        try:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total: self.total = total
                dl = d.get("downloaded_bytes") or 0
                p = dl * 100.0 / self.total if self.total else 0.0
                now = time.time()
                if now - self.last_ts < PROGRESS_EVERY and abs(p - self.last_p) < 5: return
                self.last_ts, self.last_p = now, p
                head = f"🎬 {self.label}" + (f" 💾 {human_size(self.total)}" if self.total else "")
                body = f"📥 Скачиваю:\n{make_bar(p)} {p:.0f}%"
                sp = (d.get("_speed_str") or "").strip()
                if sp and sp != "N/A": body += f"\n⚡ {sp}"
                self._post(f"{head}\n\n{body}")
            elif d.get("status") == "finished":
                head = f"🎬 {self.label}" + (f" 💾 {human_size(self.total)}" if self.total else "")
                self._post(f"{head}\n\n🔄 Обрабатываю файл…")
        except Exception: pass

# ---------- ffmpeg ----------
def fix_aspect(path: Path) -> Path:
    if not FIX_ASPECT or path.suffix.lower() not in (".mp4", ".mkv", ".mov"): return path
    fixed = path.with_name(path.stem + "_sar.mp4")
    try:
        subprocess.run(["ffmpeg","-y","-loglevel","error","-i",str(path),"-c","copy",
                        "-bsf:v","h264_metadata=sample_aspect_ratio=1/1",str(fixed)],
                       capture_output=True, timeout=60, check=True)
        if fixed.exists() and fixed.stat().st_size > 0:
            path.unlink(missing_ok=True); return fixed
    except Exception:
        fixed.unlink(missing_ok=True)
    try:
        subprocess.run(["ffmpeg","-y","-loglevel","error","-i",str(path),
                        "-vf","scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1",
                        "-c:v","libx264","-preset","veryfast","-crf","23","-c:a","copy",
                        "-movflags","+faststart",str(fixed)],
                       capture_output=True, timeout=600, check=True)
        if fixed.exists() and fixed.stat().st_size > 0:
            path.unlink(missing_ok=True); return fixed
    except Exception:
        log.warning("aspect fix failed", exc_info=True)
        fixed.unlink(missing_ok=True)
    return path

def trim_sync(path: Path, start: str, end: str) -> Path:
    out = path.with_name("trim_" + path.stem + ".mp4")
    subprocess.run(["ffmpeg","-y","-loglevel","error","-ss",start,"-to",end,
                    "-i",str(path),"-c","copy","-movflags","+faststart",str(out)],
                   capture_output=True, timeout=600, check=True)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("ffmpeg не создал файл")
    return out

# ---------- yt-dlp ----------
def base_opts(url: str) -> dict:
    p = platform_from_url(url)
    o = {"noplaylist": True, "quiet": True, "no_warnings": True,
         "retries": 3, "socket_timeout": 20}
    if COOKIES_FILE and COOKIES_FILE.exists(): o["cookiefile"] = str(COOKIES_FILE)
    if PROXY_URL and p == "youtube": o["proxy"] = PROXY_URL
    return o

def ydl_options(url: str, kind: str, tpl: str, hook=None) -> dict:
    p = platform_from_url(url)
    o = base_opts(url)
    o["outtmpl"] = tpl
    if hook: o["progress_hooks"] = [hook]
    if kind == "audio":
        o["format"] = "bestaudio/best"
        o["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                "preferredquality": "192"}]
    elif p == "youtube":
        o["format"] = YT_FORMATS.get(kind, YT_FORMATS["best"])
        o["merge_output_format"] = "mp4"
    else:
        o["format"] = SOCIAL_FORMATS.get(kind, SOCIAL_FORMATS["best"])
        o["format_sort"] = ["res", "br"]
        o["merge_output_format"] = "mp4"
    return o

def extract_info_sync(url: str) -> dict:
    o = base_opts(url); o["skip_download"] = True
    with yt_dlp.YoutubeDL(o) as y:
        return y.extract_info(url, download=False)

def search_sync(query: str, n: int = 5) -> list:
    o = {"quiet": True, "no_warnings": True, "skip_download": True,
         "extract_flat": True, "noplaylist": True}
    if COOKIES_FILE and COOKIES_FILE.exists(): o["cookiefile"] = str(COOKIES_FILE)
    with yt_dlp.YoutubeDL(o) as y:
        info = y.extract_info(f"ytsearch{n}:{query}", download=False)
    return [e for e in (info.get("entries") or []) if e]

def download_sync(url: str, kind: str, hook=None) -> Path:
    p = platform_from_url(url)
    job = DOWNLOAD_DIR / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=True)
    tpl = str(job / "%(title).90B [%(id)s].%(ext)s")
    with yt_dlp.YoutubeDL(ydl_options(url, kind, tpl, hook)) as y:
        info = y.extract_info(url, download=True)
        expected = Path(y.prepare_filename(info))
    if kind == "audio":
        mp3 = list(job.glob("*.mp3"))
        if mp3: return mp3[0]
    res = expected if expected.exists() else None
    if res is None:
        c = [x for x in job.iterdir() if x.is_file() and not x.name.endswith((".part",".ytdl"))]
        if c: res = max(c, key=lambda x: x.stat().st_size)
    if res is None: raise FileNotFoundError("файл не найден")
    if p in ("instagram","facebook","vk","tiktok","twitter"): res = fix_aspect(res)
    return res

def download_subs_sync(url: str):
    job = DOWNLOAD_DIR / ("subs_" + uuid.uuid4().hex)
    job.mkdir(parents=True, exist_ok=True)
    o = base_opts(url)
    o.update({"skip_download": True, "writesubtitles": True, "writeautomaticsub": True,
              "subtitleslangs": ["ru", "en"], "subtitlesformat": "srt",
              "outtmpl": str(job / "%(title).90B [%(id)s].%(ext)s")})
    with yt_dlp.YoutubeDL(o) as y:
        y.extract_info(url, download=True)
    files = list(job.glob("*.srt")) + list(job.glob("*.vtt"))
    if not files: raise FileNotFoundError("нет субтитров")
    return max(files, key=lambda x: x.stat().st_size)

def cleanup_file(path):
    if path is None: return
    try:
        for x in path.parent.iterdir():
            if x.is_file(): x.unlink(missing_ok=True)
        path.parent.rmdir()
    except OSError: pass

# ---------- Утилиты ----------
def truncate(t: str, n: int) -> str:
    t = (t or "").strip()
    return t if len(t) <= n else t[:n-1].rstrip() + "…"

def fmt_dur(s) -> str:
    if not s: return ""
    s = int(s); h, r = divmod(s, 3600); m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

def clean_err(e: Exception) -> str:
    return re.sub(r"\[\d+(;\d+)*m", "", str(e).replace("\x1b", "")).strip()

def fmt_uptime(s: int) -> str:
    h, r = divmod(s, 3600); m, _ = divmod(r, 60)
    return f"{h}ч {m}м"

semaphore = asyncio.Semaphore(MAX_CONCURRENT)
stats_lock = asyncio.Lock()
active_jobs = waiting_jobs = completed_jobs = 0
started_at = time.time()

async def acquire_slot():
    global active_jobs, waiting_jobs
    async with stats_lock: waiting_jobs += 1
    await semaphore.acquire()
    async with stats_lock:
        waiting_jobs -= 1; active_jobs += 1

async def release_slot(ok: bool):
    global active_jobs, completed_jobs
    async with stats_lock:
        active_jobs -= 1
        if ok: completed_jobs += 1
    semaphore.release()

# ---------- Команды ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # реферальный код
    args = ctx.args or []
    payload = args[0] if args else ""
    user_id = update.effective_user.id
    async with _ref_lock:
        me = _refs.setdefault(str(user_id), {"invited": 0, "bonus": 0})
        if payload.startswith("ref_"):
            owner = payload[4:]
            if owner.isdigit() and int(owner) != user_id:
                if not me.get("referred_by"):
                    me["referred_by"] = int(owner)
                    owner_d = _refs.setdefault(owner, {"invited": 0, "bonus": 0})
                    owner_d["invited"] = owner_d.get("invited", 0) + 1
                    if owner_d["invited"] % REF_GOAL == 0:
                        owner_d["bonus"] = owner_d.get("bonus", 0) + 1
        save_refs()

    if not await require_subscription(update, ctx):
        return
    cookie = "есть" if COOKIES_FILE and COOKIES_FILE.exists() else "нет"
    await update.message.reply_text(
        "👋 Привет! Отправь ссылку на видео — пришлю файл.\n\n"
        "▶️ YouTube  📸 Instagram  📘 Facebook\n🅥 VK  🎵 TikTok  🐦 Twitter/X\n\n"
        "🎯 Как пользоваться:\n"
        "1️⃣ Пришли ссылку\n2️⃣ Выбери качество\n3️⃣ Получи файл 🎁\n\n"
        "✨ Ещё умею:\n"
        "• 🔍 Поиск по YouTube — пришли название\n"
        "• ✂️ Обрезка — нажми «Обрезать» и пришли 00:10-00:40\n"
        "• 📝 Субтитры — файл .srt\n"
        f"• 🔗 Рефералка — пригласи {REF_GOAL} друга, получи бонус\n\n"
        f"🍪 Cookies: {cookie}",
        reply_markup=REPLY_MENU)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx): return
    await update.message.reply_text(
        "🔗 Поддерживаемые ссылки:\n"
        "• youtube.com, youtu.be\n• instagram.com/reel|p\n• facebook.com, fb.watch\n"
        "• vk.com, vkvideo.ru\n• tiktok.com\n• twitter.com, x.com\n\n"
        "🔍 Поиск: нажми «🔍 Поиск» и напиши название\n"
        "✂️ Обрезка: нажми «Обрезать» и пришли 00:10-00:40\n"
        "📝 Субтитры: кнопка «Субтитры» после ссылки\n"
        "💬 Inline: в любом чате напиши @этот_бот + ссылку",
        reply_markup=REPLY_MENU)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx): return
    async with stats_lock: a, w, c = active_jobs, waiting_jobs, completed_jobs
    inv, bonus = ref_stats(update.effective_user.id)
    await update.message.reply_text(
        f"📊 Статус\nАктивных: {a}/{MAX_CONCURRENT}\nВ очереди: {w}\n"
        f"Скачано: {c}\nПриглашено: {inv} (бонусов: {bonus})\n"
        f"Аптайм: {fmt_uptime(int(time.time()-started_at))}",
        reply_markup=REPLY_MENU)

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=REPLY_MENU)

async def cmd_ref(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx): return
    uid = update.effective_user.id
    link = ref_link(ctx.bot.username, uid)
    inv, bonus = ref_stats(uid)
    await update.message.reply_text(
        f"🔗 Твоя реферальная ссылка:\n{link}\n\n"
        f"👥 Приглашено: {inv}\n🎁 Бонусов: {bonus}\n\n"
        f"За каждые {REF_GOAL} приглашения — +1 бонус.",
        reply_markup=REPLY_MENU)

# ---------- Показ превью + меню ----------
async def show_video_menu(update, ctx, platform, url):
    msg = await update.effective_message.reply_text("🔍 Получаю информацию…")
    loop = asyncio.get_running_loop()
    info = None
    try:
        info = await asyncio.wait_for(loop.run_in_executor(None, extract_info_sync, url), timeout=40)
    except asyncio.TimeoutError:
        log.warning("info timeout")
    except Exception:
        log.warning("info failed", exc_info=True)

    if info:
        title = truncate(info.get("title") or "Видео", 60)
        up = truncate(info.get("uploader") or info.get("channel") or "", 40)
        dur = fmt_dur(info.get("duration"))
        lines = [f"{PLATFORM_NAMES[platform]}: {title}"]
        if up: lines.append(f"👤 {up}")
        if dur: lines.append(f"⏱ {dur}")
    else:
        lines = [f"{PLATFORM_NAMES[platform]}: видео"]
    lines += ["", "Выберите качество:"]
    caption = "\n".join(lines)

    await msg.delete()
    thumb = info.get("thumbnail") if info else None
    if thumb:
        data = await loop.run_in_executor(None, fetch_thumb_bytes, thumb, platform)
        if data:
            try:
                await update.effective_message.reply_photo(
                    photo=data, caption=caption, reply_markup=QUALITY_MENU)
                return
            except Exception:
                log.warning("photo failed")
    await update.effective_message.reply_text(caption, reply_markup=QUALITY_MENU)

# ---------- Обработка текста ----------
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx): return
    text = (update.message.text or "").strip()

    # ожидание диапазона обрезки
    if ctx.user_data.get("await_trim"):
        url = ctx.user_data.get("trim_url")
        m = re.match(r"^\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—]\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*$", text)
        if not m or not url:
            await update.message.reply_text("Формат: 00:10-00:40. Пришли диапазон.", reply_markup=REPLY_MENU)
            return
        ctx.user_data.pop("await_trim", None)
        ctx.user_data.pop("trim_url", None)
        await trim_flow(update, ctx, url, m.group(1), m.group(2))
        return

    # ожидание поискового запроса
    if ctx.user_data.get("await_search"):
        ctx.user_data.pop("await_search", None)
        await do_search(update, ctx, text)
        return

    if text == "🎬 Скачать видео":
        await update.message.reply_text("Пришли ссылку на видео.", reply_markup=REPLY_MENU); return
    if text == "🎵 MP3":
        await update.message.reply_text("Пришли ссылку; затем выбери MP3.", reply_markup=REPLY_MENU); return
    if text == "🔍 Поиск":
        ctx.user_data["await_search"] = True
        await update.message.reply_text("🔍 Что искать на YouTube? Напиши название.", reply_markup=REPLY_MENU); return
    if text == "🔗 Моя ссылка":
        await cmd_ref(update, ctx); return
    if text == "ℹ️ Помощь":
        await cmd_help(update, ctx); return
    if text == "📊 Статус":
        await cmd_status(update, ctx); return

    platform, url = detect_platform(text)
    if platform:
        ctx.user_data["url"] = url
        ctx.user_data["platform"] = platform
        await show_video_menu(update, ctx, platform, url)
        return

    # не ссылка — считаем поисковым запросом
    await do_search(update, ctx, text)

# ---------- Поиск ----------
async def do_search(update, ctx, query: str):
    if not query or len(query) < 2:
        await update.effective_message.reply_text("Слишком короткий запрос.", reply_markup=REPLY_MENU); return
    msg = await update.effective_message.reply_text("🔍 Ищу на YouTube…")
    loop = asyncio.get_running_loop()
    try:
        results = await asyncio.wait_for(loop.run_in_executor(None, search_sync, query, 5), timeout=40)
    except Exception:
        log.warning("search failed", exc_info=True)
        await msg.edit_text("Не удалось выполнить поиск. Попробуй позже.")
        return
    if not results:
        await msg.edit_text("Ничего не нашёл. Попробуй другой запрос.")
        return
    await msg.delete()
    ctx.user_data["search_results"] = []
    rows = []
    for i, e in enumerate(results[:5]):
        title = truncate(e.get("title") or "Видео", 60)
        vid = e.get("id")
        url = f"https://www.youtube.com/watch?v={vid}" if vid else e.get("url")
        if not url: continue
        ctx.user_data["search_results"].append(url)
        idx = len(ctx.user_data["search_results"]) - 1
        rows.append([InlineKeyboardButton(f"{i+1}. {title[:55]}", callback_data=f"s:{idx}")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="s:cancel")])
    await update.effective_message.reply_text(
        "🔍 Нашёл на YouTube:\nНажми на видео, чтобы скачать.",
        reply_markup=InlineKeyboardMarkup(rows))

# ---------- Обрезка ----------
async def trim_flow(update, ctx, url, start, end):
    await download_and_send(update, ctx, url, "best", trim=(start, end))

# ---------- Скачивание ----------
async def download_and_send(update, ctx, url, requested_kind, via_callback=False, trim=None):
    query = update.callback_query if via_callback else None
    message = query.message if query else update.effective_message
    chat_id = message.chat_id
    platform = platform_from_url(url)

    if platform == "youtube":
        attempts = [requested_kind] + YT_FALLBACKS.get(requested_kind, [])
    elif requested_kind == "audio":
        attempts = ["audio"]
    else:
        attempts = SOCIAL_ORDER[SOCIAL_ORDER.index(requested_kind):] if requested_kind in SOCIAL_ORDER else list(SOCIAL_ORDER)

    await acquire_slot()
    ok = False
    loop = asyncio.get_running_loop()
    try:
        for idx, kind in enumerate(attempts):
            label = LABELS.get(kind, kind)
            head = f"🎬 {label}\n\n📥 Готовлю загрузку…"
            if idx: head = f"🎬 {label}\n\n⏳ Предыдущий формат не подошёл, пробую снова…"
            if via_callback:
                try:
                    await query.edit_message_text(head); pmsg = query.message
                except Exception:
                    pmsg = await ctx.bot.send_message(chat_id, head)
            else:
                pmsg = await message.reply_text(head)

            async def upd(t, _m=pmsg):
                try:
                    await ctx.bot.edit_message_text(chat_id=_m.chat_id, message_id=_m.message_id, text=t)
                except Exception: pass

            hook = ProgressHook(loop, upd, label)
            await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
            path = None
            try:
                path = await loop.run_in_executor(None, download_sync, url, kind, hook)
                if trim:
                    try:
                        trimmed = await loop.run_in_executor(None, trim_sync, path, trim[0], trim[1])
                        cleanup_file(path); path = trimmed
                    except Exception:
                        log.warning("trim failed", exc_info=True)
                        await ctx.bot.send_message(chat_id, "Не удалось обрезать. Отправляю полное видео.")
                size_mb = path.stat().st_size / (1024 * 1024)
                if size_mb > MAX_SIZE_MB:
                    cleanup_file(path); path = None; continue

                try:
                    await ctx.bot.edit_message_text(chat_id=pmsg.chat_id, message_id=pmsg.message_id,
                        text=f"🎬 {label} 💾 {human_size(path.stat().st_size)}\n\n📤 Загружаю…")
                except Exception: pass

                with path.open("rb") as f:
                    if kind == "audio":
                        await ctx.bot.send_audio(chat_id=chat_id, audio=f, title=path.stem, caption="🎵 Готово!")
                    else:
                        await ctx.bot.send_video(chat_id=chat_id, video=f, supports_streaming=True,
                                                 caption=f"🎬 Готово! ({label})")
                ok = True
                try: await ctx.bot.delete_message(pmsg.chat_id, pmsg.message_id)
                except Exception: pass
                await ctx.bot.send_message(chat_id, "Что дальше?", reply_markup=REPLY_MENU)
                return
            except yt_dlp.utils.DownloadError as e:
                log.warning("format %s: %s", kind, clean_err(e))
                try: await ctx.bot.delete_message(pmsg.chat_id, pmsg.message_id)
                except Exception: pass
                if idx == len(attempts) - 1:
                    raise
            finally:
                cleanup_file(path)

        await ctx.bot.send_message(chat_id, unavailable_message(), reply_markup=REPLY_MENU)

    except yt_dlp.utils.DownloadError as e:
        log.warning("download failed: %s", clean_err(e))
        await ctx.bot.send_message(chat_id, unavailable_message(), reply_markup=REPLY_MENU)
    except Exception:
        log.exception("download error")
        await ctx.bot.send_message(chat_id, unavailable_message(), reply_markup=REPLY_MENU)
    finally:
        await release_slot(ok)

# ---------- Субтитры ----------
async def send_subs(update, ctx, url):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_text("📝 Ищу субтитры…")
    except Exception: pass
    loop = asyncio.get_running_loop()
    path = None
    try:
        path = await asyncio.wait_for(loop.run_in_executor(None, download_subs_sync, url), timeout=90)
        with path.open("rb") as f:
            await ctx.bot.send_document(chat_id=query.message.chat_id, document=f,
                                        filename=path.name, caption="📝 Субтитры")
        try: await query.message.delete()
        except Exception: pass
        await ctx.bot.send_message(query.message.chat_id, "Что дальше?", reply_markup=REPLY_MENU)
    except Exception:
        log.warning("subs failed", exc_info=True)
        try:
            await query.edit_message_text("❌ Субтитры не найдены для этого видео.")
        except Exception: pass
    finally:
        cleanup_file(path)

# ---------- Кнопки ----------
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""

    if data == "sub:check":
        if await check_subscription(ctx.bot, q.from_user.id):
            await q.answer("Подписка подтверждена")
            await q.message.reply_text("Спасибо! Теперь отправь ссылку.", reply_markup=REPLY_MENU)
        else:
            await q.answer("Сначала подпишись на канал.", show_alert=True)
        return

    await q.answer()

    # поиск
    if data.startswith("s:"):
        val = data[2:]
        if val == "cancel":
            try: await q.edit_message_text("Поиск отменён.")
            except Exception: pass
            return
        results = ctx.user_data.get("search_results") or []
        if not val.isdigit() or int(val) >= len(results):
            try: await q.edit_message_text("Результат устарел. Попробуй поиск снова.")
            except Exception: pass
            return
        url = results[int(val)]
        await download_and_send(update, ctx, url, "best", via_callback=True)
        return

    action = data.removeprefix("q:")

    if action == "cancel":
        ctx.user_data.clear()
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        try: await q.edit_message_text("❌ Отменено.")
        except Exception: pass
        return

    if not await check_subscription(ctx.bot, q.from_user.id):
        await q.message.reply_text(f"Подпишись на {CHANNEL}.", reply_markup=sub_keyboard())
        return

    url = ctx.user_data.get("url")
    if not url:
        try: await q.edit_message_text("Ссылка устарела. Пришли её заново.")
        except Exception: pass
        return

    if action == "subs":
        await send_subs(update, ctx, url); return

    if action == "trim":
        ctx.user_data["await_trim"] = True
        ctx.user_data["trim_url"] = url
        try: await q.edit_message_text("✂️ Пришли диапазон, например: 00:10-00:40")
        except Exception: pass
        return

    await download_and_send(update, ctx, url, action, via_callback=True)

# ---------- Inline ----------
async def on_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query
    query = (q.query or "").strip()
    platform, url = detect_platform(query)
    bot_uname = ctx.bot.username or ""
    if not url:
        results = [InlineQueryResultArticle(
            id="hint", title="🎬 Пришлите ссылку",
            description="Вставьте ссылку на видео YouTube, TikTok, VK, Instagram…",
            input_message_content=InputTextMessageContent(
                f"🎬 Скачиваю видео в @{bot_uname}"))]
        await q.answer(results, cache_time=5)
        return
    results = [InlineQueryResultArticle(
        id="dl", title=f"🎬 Скачать видео ({platform})",
        description="Открой бота и пришли ссылку — пришлю файл",
        input_message_content=InputTextMessageContent(
            f"🎬 Скачать это видео:\n{url}\n\n👉 @{bot_uname}"))]
    if len(query) < 90:
        results.append(InlineQueryResultArticle(
            id="same", title="🔁 Открыть в боте",
            description="Нажми, чтобы перейти в бота",
            input_message_content=InputTextMessageContent(
                f"👉 Открой @{bot_uname} и пришли ссылку:\n{url}")))
    await q.answer(results, cache_time=5)

# ---------- health ----------
def start_health_server():
    port = int(os.getenv("PORT", "0"))
    if not port: return

    class H(BaseHTTPRequestHandler):
        def _ok(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "19")
            self.end_headers()
            if body: self.wfile.write(b"OK - bot is running")
        def do_GET(self): self._ok(True)
        def do_HEAD(self): self._ok(False)
        def log_message(self, *a): pass

    HTTPServer(("0.0.0.0", port), H).serve_forever()

# ---------- Запуск ----------
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "🏠 Меню"),
        BotCommand("help", "ℹ️ Помощь"),
        BotCommand("ref", "🔗 Моя ссылка"),
        BotCommand("status", "📊 Статус"),
        BotCommand("cancel", "❌ Отмена"),
    ])

def main():
    if not TOKEN: raise SystemExit("Не задана переменная BOT_TOKEN.")
    load_refs()
    if os.getenv("PORT"):
        Thread(target=start_health_server, daemon=True).start()

    app = (Application.builder()
           .token(TOKEN).post_init(post_init)
           .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
           .get_updates_connect_timeout(30).get_updates_read_timeout(30).build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("ref", cmd_ref))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(q:|sub:|s:)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Бот запускается. Прокси: %s", bool(PROXY_URL))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
