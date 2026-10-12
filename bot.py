# bot.py — YouTube + Instagram + Facebook Telegram bot
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

# ---------- Настройки (значения берутся из Render Environment) ----------
TOKEN = os.getenv("BOT_TOKEN")
MAX_SIZE_MB = int(os.getenv("MAX_SIZE_MB", "48"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
PROXY_URL = os.getenv("PROXY_URL", "").strip()

CHANNEL = "@edrikevideo"
CHANNEL_URL = "https://t.me/edrikevideo"

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ytdl_bot"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ytdl-bot")

# ---------- Cookies ----------
LOCAL_COOKIES = Path(__file__).parent / "cookies.txt"
SECRET_COOKIES = Path("/etc/secrets/cookies.txt")
TEMP_COOKIES = Path(tempfile.gettempdir()) / "ytdl_bot_cookies.txt"

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

# ---------- Платформы ----------
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


def platform_from_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return "youtube"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram"
    if host == "facebook.com" or host.endswith(".facebook.com"):
        return "facebook"
    if host == "fb.watch" or host.endswith(".fb.watch"):
        return "facebook"
    return "other"


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
    platform = platform_from_url(url)

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

    # Прокси используется только для YouTube
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
    else:
        # Instagram/Facebook обычно отдают готовую видеодорожку со звуком
        opts["format"] = "best[ext=mp4]/best"
        opts["merge_output_format"] = "mp4"

    return opts


def extract_info_sync(url: str) -> dict:
    platform = platform_from_url(url)
    opts = {
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 45,
    }
    if COOKIES_FILE and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if PROXY_URL and platform == "youtube":
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


# ---------- Форматирование и состояние ----------
def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def format_duration(seconds) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def clean_error(error: Exception) -> str:
    text = str(error).replace("\x1b", "")
    return re.sub(r"\[\d+(;\d+)*m", "", text).strip()


def format_uptime(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}ч {minutes}м"


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


async def edit_status(query, text: str):
    try:
        await query.edit_message_text(text)
    except Exception:
        try:
            await query.edit_message_caption(caption=text)
        except Exception:
            await query.message.reply_text(text)


# ---------- Команды бота ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx):
        return

    cookie_status = "есть" if COOKIES_FILE and COOKIES_FILE.exists() else "нет"
    proxy_status = "включён для YouTube" if PROXY_URL else "выключен"

    await update.message.reply_text(
        "👋 Привет! Отправьте ссылку на YouTube, Instagram или Facebook.\n"
        "После ссылки покажу доступные варианты.\n\n"
        f"Лимит отправки Telegram: около {MAX_SIZE_MB} МБ.\n"
        f"Cookies: {cookie_status}.\n"
        f"Прокси: {proxy_status}.",
        reply_markup=REPLY_MENU,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx):
        return
    await update.message.reply_text(
        "Отправьте ссылку на видео — бот покажет доступные варианты.\n"
        "YouTube: выбор качества и MP3.\n"
        "Instagram/Facebook: видео или MP3.\n"
        "Большие видео бот попробует загрузить в более низком качестве.",
        reply_markup=REPLY_MENU,
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx):
        return
    async with stats_lock:
        active, waiting, completed = active_jobs, waiting_jobs, completed_jobs

    cookie_status = "✅ загружены" if COOKIES_FILE and COOKIES_FILE.exists() else "❌ нет"
    proxy_status = "✅ включён для YouTube" if PROXY_URL else "❌ выключен"

    await update.message.reply_text(
        f"📊 Статус\n"
        f"Активных задач: {active}/{MAX_CONCURRENT}\n"
        f"В очереди: {waiting}\n"
        f"Успешно отправлено: {completed}\n"
        f"Cookies: {cookie_status}\n"
        f"Прокси: {proxy_status}\n"
        f"Аптайм: {format_uptime(int(time.time() - started_at))}",
        reply_markup=REPLY_MENU,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("url", None)
    ctx.user_data.pop("platform", None)
    await update.message.reply_text("Отменено.", reply_markup=REPLY_MENU)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, ctx):
        return

    text = (update.message.text or "").strip()

    if text == "🎬 Скачать видео":
        await update.message.reply_text("Пришлите ссылку на видео.", reply_markup=REPLY_MENU)
        return
    if text == "🎵 Скачать MP3":
        await update.message.reply_text("Пришлите ссылку; затем выберите MP3.", reply_markup=REPLY_MENU)
        return
    if text == "ℹ️ Помощь":
        await cmd_help(update, ctx)
        return
    if text == "📊 Статус":
        await cmd_status(update, ctx)
        return
    if text == "❌ Отмена":
        await cmd_cancel(update, ctx)
        return

    platform, url = detect_platform(text)
    if not platform:
        await update.message.reply_text(
            "Не распознал ссылку. Поддерживаются YouTube, Instagram и Facebook.",
            reply_markup=REPLY_MENU,
        )
        return

    ctx.user_data["url"] = url
    ctx.user_data["platform"] = platform

    info_message = await update.message.reply_text("🔍 Получаю информацию…")
    try:
        info = await asyncio.get_running_loop().run_in_executor(
            None, extract_info_sync, url
        )
    except Exception as error:
        await info_message.edit_text(
            f"❌ Не удалось получить информацию:\n{clean_error(error)}"
        )
        return

    title = truncate(info.get("title") or "Видео", 60)
    uploader = truncate(info.get("uploader") or info.get("channel") or "", 40)
    duration = format_duration(info.get("duration"))

    caption_lines = [f"{PLATFORM_NAMES[platform]}: {title}"]
    if uploader:
        caption_lines.append(f"👤 {uploader}")
    if duration:
        caption_lines.append(f"⏱ {duration}")
    caption_lines.append("")
    caption_lines.append(
        "Выберите качество:" if platform == "youtube" else "Выберите действие:"
    )
    caption = "\n".join(caption_lines)
    keyboard = YOUTUBE_MENU if platform == "youtube" else SOCIAL_MENU

    await info_message.delete()
    thumbnail = info.get("thumbnail")
    if thumbnail:
        try:
            await update.message.reply_photo(
                photo=thumbnail,
                caption=caption,
                reply_markup=keyboard,
            )
            return
        except Exception:
            log.exception("Не удалось отправить превью")

    await update.message.reply_text(caption, reply_markup=keyboard)


# ---------- Кнопки ----------
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    if data == "sub:check":
        if await check_subscription(ctx.bot, query.from_user.id):
            await query.answer("Подписка подтверждена")
            await query.message.reply_text(
                "Спасибо! Теперь отправьте ссылку.", reply_markup=REPLY_MENU
            )
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
        await edit_status(
            query,
            "⬅️ Пришлите новую ссылку." if action == "back" else "❌ Отменено.",
        )
        return

    if not await check_subscription(ctx.bot, query.from_user.id):
        await query.message.reply_text(
            "Чтобы пользоваться ботом, подпишитесь на канал @edrikevideo.",
            reply_markup=subscription_keyboard(),
        )
        return

    url = ctx.user_data.get("url")
    if not url:
        await edit_status(query, "Ссылка устарела. Пришлите её ещё раз.")
        return

    await download_and_send(update, ctx, url, action, via_callback=True)


# ---------- Скачивание, авто-понижение и отправка ----------
async def download_and_send(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    url: str,
    requested_kind: str,
    via_callback: bool = False,
):
    query = update.callback_query if via_callback else None
    message = query.message if query else update.effective_message
    chat_id = message.chat_id
    platform = platform_from_url(url)

    if platform == "youtube":
        attempts = [requested_kind] + YOUTUBE_FALLBACKS.get(requested_kind, [])
    elif requested_kind == "audio":
        attempts = ["audio"]
    else:
        attempts = ["best", "audio"]

    await acquire_slot()
    success = False

    try:
        for index, kind in enumerate(attempts):
            label = QUALITY_LABELS.get(kind, kind)
            status = f"⏳ Загружаю {label}…"
            if index:
                status = f"Файл слишком большой или формат недоступен. Пробую {label}…"

            status_message = None
            if via_callback:
                await edit_status(query, status)
            else:
                status_message = await message.reply_text(status)

            await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)

            path = None
            try:
                path = await asyncio.get_running_loop().run_in_executor(
                    None, download_sync, url, kind
                )
                size_mb = path.stat().st_size / (1024 * 1024)

                if size_mb > MAX_SIZE_MB:
                    cleanup_file(path)
                    path = None
                    continue

                with path.open("rb") as media:
                    if kind == "audio":
                        await ctx.bot.send_audio(
                            chat_id=chat_id,
                            audio=media,
                            title=path.stem,
                            caption="🎵 Готово!",
                        )
                    else:
                        await ctx.bot.send_video(
                            chat_id=chat_id,
                            video=media,
                            supports_streaming=True,
                            caption=f"🎬 Готово! ({label})",
                        )

                success = True
                if via_callback:
                    try:
                        await query.message.delete()
                    except Exception:
                        pass
                elif status_message is not None:
                    try:
                        await status_message.delete()
                    except Exception:
                        pass

                await ctx.bot.send_message(
                    chat_id, "Что дальше?", reply_markup=REPLY_MENU
                )
                return

            except yt_dlp.utils.DownloadError as error:
                log.warning("Формат %s недоступен: %s", kind, clean_error(error))
                if index == len(attempts) - 1:
                    raise
            finally:
                cleanup_file(path)

        await ctx.bot.send_message(
            chat_id,
            f"❌ Не удалось подобрать файл меньше {MAX_SIZE_MB} МБ.",
            reply_markup=REPLY_MENU,
        )

    except Exception as error:
        log.exception("Ошибка скачивания")
        await ctx.bot.send_message(
            chat_id,
            f"❌ Не удалось скачать:\n{clean_error(error)}",
            reply_markup=REPLY_MENU,
        )
    finally:
        await release_slot(success)


# ---------- HTTP health check для Render ----------
def start_health_server():
    port = int(os.getenv("PORT", "0"))
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK - bot is running")

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

    log.info("Бот запускается. Прокси настроен: %s", bool(PROXY_URL))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
