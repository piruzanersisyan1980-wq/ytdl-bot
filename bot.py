# bot.py — YouTube + Instagram + Facebook
import os
import re
import time
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path
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

# ===== НАСТРОЙКИ =====
TOKEN          = os.getenv("BOT_TOKEN")
MAX_SIZE_MB    = int(os.getenv("MAX_SIZE_MB", "50"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
MAX_TITLE_LEN  = int(os.getenv("MAX_TITLE_LEN", "60"))
DOWNLOAD_DIR   = Path(tempfile.gettempdir()) / "ytdl_bot"
DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ytdl-bot")

# ===== COOKIES =====
COOKIES_FILE = Path(__file__).parent / "cookies.txt"
SECRET_COOKIES = Path("/etc/secrets/cookies.txt")
if not COOKIES_FILE.exists() and SECRET_COOKIES.exists():
    try:
        _tmp = Path(tempfile.gettempdir()) / "cookies.txt"
        shutil.copyfile(SECRET_COOKIES, _tmp)
        COOKIES_FILE = _tmp
    except Exception as _e:
        log.error("Не скопировать cookies: %s", _e)
        COOKIES_FILE = SECRET_COOKIES

log.info("COOKIES_FILE = %s (exists=%s)", COOKIES_FILE, COOKIES_FILE.exists())
# ====================

# ===== РАСПОЗНАВАНИЕ ССЫЛОК ПО ПЛАТФОРМАМ =====
PLATFORM_PATTERNS = [
    ("youtube",   re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/[\w\-?=&/%.]+")),
    ("instagram", re.compile(r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv|share)/[\w\-?=&/%.]+")),
    ("facebook",  re.compile(r"https?://(?:www\.|m\.|web\.|ru-ru\.)?(?:facebook\.com|fb\.watch|fb\.com)/[\w\-?=&/%.]+")),
]

PLATFORM_EMOJI = {
    "youtube":   "▶️ YouTube",
    "instagram": "📸 Instagram",
    "facebook":  "📘 Facebook",
}


def detect_platform(text: str):
    """Возвращает (platform, url) или (None, None)."""
    for name, rx in PLATFORM_PATTERNS:
        m = rx.search(text)
        if m:
            return name, m.group(0)
    return None, None


# ===== КЛАВИАТУРЫ =====
REPLY_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🎬 Скачать видео")],
        [KeyboardButton("🎵 Скачать MP3"), KeyboardButton("ℹ️ Помощь")],
        [KeyboardButton("📊 Статус"),      KeyboardButton("❌ Отмена")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Вставьте ссылку — YouTube / Instagram / Facebook…",
)

# Для YouTube — расширенное меню качества
YT_QUALITY_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 1080p", callback_data="q:1080"),
     InlineKeyboardButton("🎬 720p",  callback_data="q:720")],
    [InlineKeyboardButton("🎬 480p",  callback_data="q:480"),
     InlineKeyboardButton("🎬 360p",  callback_data="q:360")],
    [InlineKeyboardButton("⭐ Лучшее", callback_data="q:best"),
     InlineKeyboardButton("🎵 MP3",    callback_data="q:audio")],
    [InlineKeyboardButton("⬅️ Назад",  callback_data="q:back"),
     InlineKeyboardButton("❌ Отмена", callback_data="q:cancel")],
])

# Для Instagram / Facebook — простое меню (обычно один формат)
SOCIAL_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎬 Скачать видео", callback_data="q:best")],
    [InlineKeyboardButton("🎵 MP3",           callback_data="q:audio")],
    [InlineKeyboardButton("⬅️ Назад",         callback_data="q:back"),
     InlineKeyboardButton("❌ Отмена",        callback_data="q:cancel")],
])

EMPTY_KB = InlineKeyboardMarkup([])
# =====================

# ===== yt-dlp =====
QUALITY_FORMATS = {
    "1080": "bv*[height<=1080]+ba/b[height<=1080]",
    "720":  "bv*[height<=720]+ba/b[height<=720]",
    "480":  "bv*[height<=480]+ba/b[height<=480]",
    "360":  "bv*[height<=360]+ba/b[height<=360]",
    "best": "bv*+ba/b",
    "audio": "bestaudio/best",
}


def _base_opts() -> dict:
    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "socket_timeout": 30,
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def _ydl_opts(kind: str, out_tpl: str) -> dict:
    opts = _base_opts()
    opts["outtmpl"] = out_tpl
    if kind == "audio":
        opts.update({
            "format": QUALITY_FORMATS["audio"],
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    else:
        opts.update({
            "format": QUALITY_FORMATS.get(kind, QUALITY_FORMATS["720"]),
            "merge_output_format": "mp4",
        })
    return opts


def _extract_info_sync(url: str) -> dict:
    opts = _base_opts()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def download_sync(url: str, kind: str) -> Path:
    job_id = str(abs(hash(url + kind)) % (10**10))
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    out_tpl = str(job_dir / "%(title).120B [%(id)s].%(ext)s")

    with yt_dlp.YoutubeDL(_ydl_opts(kind, out_tpl)) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))

    if kind == "audio":
        path = path.with_suffix(".mp3")

    if not path.exists():
        found = list(job_dir.glob("*"))
        if len(found) == 1:
            path = found[0]
        else:
            raise FileNotFoundError(f"Файл не найден: {path}")
    return path


# ===== утилиты =====
def _truncate(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _fmt_duration(sec) -> str:
    if not sec:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_uptime(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}ч {m}м {s}с"


def _clean_err(e: Exception) -> str:
    text = str(e).replace("\x1b", "")
    text = re.sub(r"\[\d+(;\d+)*m", "", text)
    return text.strip()


async def _set_status(query, text: str):
    try:
        await query.edit_message_text(text); return
    except Exception:
        pass
    try:
        await query.edit_message_caption(caption=text); return
    except Exception:
        pass
    await query.message.reply_text(text)


async def _clear_keyboard(query):
    try:
        await query.edit_message_reply_markup(reply_markup=EMPTY_KB)
    except Exception:
        pass


# ===== очередь =====
_semaphore  = asyncio.Semaphore(MAX_CONCURRENT)
_stats_lock = asyncio.Lock()
_active     = 0
_waiting    = 0
_finished   = 0
_started_at = time.time()


async def _acquire_slot():
    global _active, _waiting
    async with _stats_lock:
        _waiting += 1
    await _semaphore.acquire()
    async with _stats_lock:
        _waiting -= 1
        _active += 1


async def _release_slot():
    global _active, _finished
    async with _stats_lock:
        _active -= 1
        _finished += 1
    _semaphore.release()


# ===== команды =====
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cookies_note = "🍪 Cookies: загружены" if COOKIES_FILE.exists() else "⚠️ Cookies: НЕТ"
    await update.message.reply_text(
        "👋 Привет! Я качаю видео и музыку.\n\n"
        "**Поддерживаю:**\n"
        "▶️ YouTube (видео, Shorts, эфиры)\n"
        "📸 Instagram (посты, Reels)\n"
        "📘 Facebook (видео, Reels)\n\n"
        "**Как пользоваться:**\n"
        "1️⃣ Пришлите ссылку.\n"
        "2️⃣ Выберите качество кнопкой.\n"
        "3️⃣ Получите файл. 🎁\n\n"
        f"⚠️ Лимит файла — {MAX_SIZE_MB} МБ.\n"
        f"{cookies_note}",
        parse_mode="Markdown",
        reply_markup=REPLY_MENU,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Справка**\n\n"
        "**Поддерживаемые ссылки:**\n"
        "• `youtube.com/watch?v=…` / `youtu.be/…`\n"
        "• `youtube.com/shorts/…`\n"
        "• `instagram.com/p/…`, `instagram.com/reel/…`\n"
        "• `facebook.com/…`, `fb.watch/…`\n\n"
        "**Кнопки качества:**\n"
        "• На YouTube — 1080p / 720p / 480p / 360p / MP3\n"
        "• На Instagram/Facebook — видео / MP3\n\n"
        "⚠️ **Instagram и Facebook** часто требуют cookies — "
        "если видео не скачивается, обновите `cookies.txt`.",
        parse_mode="Markdown",
        reply_markup=REPLY_MENU,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("url", None)
    ctx.user_data.pop("platform", None)
    await update.message.reply_text("Отменено.", reply_markup=REPLY_MENU)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async with _stats_lock:
        a, w, f = _active, _waiting, _finished
    uptime = int(time.time() - _started_at)
    cookies_state = "✅ загружены" if COOKIES_FILE.exists() else "❌ нет"
    await update.message.reply_text(
        f"📊 **Статус бота**\n\n"
        f"🟢 Активных задач: {a}/{MAX_CONCURRENT}\n"
        f"⏳ В очереди: {w}\n"
        f"✅ Всего скачано: {f}\n"
        f"🍪 Cookies: {cookies_state}\n"
        f"⏱ Аптайм: {_fmt_uptime(uptime)}",
        parse_mode="Markdown",
        reply_markup=REPLY_MENU,
    )


# ===== кнопки нижней панели =====
async def handle_menu_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "🎬 Скачать видео":
        await update.message.reply_text(
            "🎬 Пришлите ссылку — покажу варианты качества.\n"
            "YouTube / Instagram / Facebook.",
            reply_markup=REPLY_MENU,
        )
        return
    if text == "🎵 Скачать MP3":
        await update.message.reply_text(
            "🎵 Пришлите ссылку — покажу варианты (в том числе MP3).",
            reply_markup=REPLY_MENU,
        )
        return
    if text == "ℹ️ Помощь":
        await cmd_help(update, ctx); return
    if text == "📊 Статус":
        await cmd_status(update, ctx); return
    if text == "❌ Отмена":
        await cmd_cancel(update, ctx); return

    await handle_link(update, ctx)


# ===== обработка ссылки + превью =====
async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    platform, url = detect_platform(text)
    if not platform:
        await update.message.reply_text(
            "🤔 Это не похоже на ссылку с поддерживаемых сайтов.\n\n"
            "Поддерживаю: **YouTube**, **Instagram**, **Facebook**.",
            parse_mode="Markdown",
            reply_markup=REPLY_MENU,
        )
        return

    ctx.user_data["url"] = url
    ctx.user_data["platform"] = platform

    placeholder = await update.message.reply_text("🔍 Получаю информацию…")
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)

    try:
        info = await asyncio.get_running_loop().run_in_executor(None, _extract_info_sync, url)
    except Exception as e:
        await placeholder.edit_text(f"❌ Не удалось получить видео:\n{_clean_err(e)}")
        return

    title    = _truncate(info.get("title") or "Видео", MAX_TITLE_LEN)
    uploader = _truncate(info.get("uploader") or info.get("channel") or "", 40)
    dur      = _fmt_duration(info.get("duration"))
    thumb    = info.get("thumbnail")

    lines = [f"{PLATFORM_EMOJI.get(platform, '🎬')} {title}"]
    if uploader: lines.append(f"👤 {uploader}")
    if dur:      lines.append(f"⏱ {dur}")
    lines.append("")
    lines.append("Выберите:" if platform != "youtube" else "Выберите качество:")
    caption = "\n".join(lines)

    # Разное меню для разных платформ
    menu = YT_QUALITY_MENU if platform == "youtube" else SOCIAL_MENU

    await placeholder.delete()
    if thumb:
        try:
            await update.message.reply_photo(photo=thumb, caption=caption, reply_markup=menu)
            return
        except Exception as e:
            log.warning("Не отправить превью: %s", e)

    await update.message.reply_text(caption, reply_markup=menu)


# ===== инлайн-кнопки качества =====
async def on_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    kind = query.data.split(":", 1)[1]

    if kind == "back":
        ctx.user_data.pop("url", None)
        ctx.user_data.pop("platform", None)
        try:
            await query.edit_message_caption(caption="⬅️ Ок. Пришлите новую ссылку.", reply_markup=EMPTY_KB)
            return
        except Exception:
            pass
        await _clear_keyboard(query)
        await _set_status(query, "⬅️ Ок. Пришлите новую ссылку.")
        return

    if kind == "cancel":
        ctx.user_data.pop("url", None)
        ctx.user_data.pop("platform", None)
        await _clear_keyboard(query)
        await _set_status(query, "❌ Отменено.")
        return

    url = ctx.user_data.get("url")
    if not url:
        await _set_status(query, "Ссылка потерялась, пришлите её заново.")
        return

    await _clear_keyboard(query)
    await _set_status(query, f"⏳ Готовлю скачивание ({kind})…")
    await _do_download(update, ctx, url, kind, via_callback=True)


# ===== скачивание и отправка =====
async def _do_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                       url: str, kind: str, via_callback: bool = False):
    global _active, _waiting

    query = update.callback_query if via_callback else None
    msg = query.message if via_callback else update.message
    chat_id = msg.chat_id

    async with _stats_lock:
        active_now, waiting_now = _active, _waiting
    if active_now >= MAX_CONCURRENT:
        queue_text = (
            f"⏳ Все слоты заняты ({active_now}/{MAX_CONCURRENT}).\n"
            f"Ваша позиция в очереди: {waiting_now + 1}"
        )
        if via_callback:
            await _set_status(query, queue_text)
        else:
            await msg.reply_text(queue_text, reply_markup=REPLY_MENU)

    await _acquire_slot()
    path = None
    try:
        await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
        loop = asyncio.get_running_loop()
        path = await loop.run_in_executor(None, download_sync, url, kind)

        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_SIZE_MB:
            text = (f"❌ Файл {size_mb:.1f} МБ — больше лимита Telegram "
                    f"({MAX_SIZE_MB} МБ). Попробуйте MP3.")
            if via_callback:
                await _set_status(query, text)
            else:
                await msg.reply_text(text, reply_markup=REPLY_MENU)
            return

        if via_callback:
            try:
                await ctx.bot.delete_message(chat_id, msg.message_id)
            except Exception:
                pass

        with open(path, "rb") as f:
            if kind == "audio":
                await ctx.bot.send_audio(chat_id=chat_id, audio=f,
                                         title=path.stem, caption="🎵 Готово!")
            else:
                await ctx.bot.send_video(chat_id=chat_id, video=f,
                                         supports_streaming=True, caption="🎬 Готово!")

        await ctx.bot.send_message(chat_id, "Что дальше?", reply_markup=REPLY_MENU)

    except yt_dlp.utils.DownloadError as e:
        text = f"❌ Не удалось скачать:\n{_clean_err(e)}"
        if via_callback:
            await ctx.bot.send_message(chat_id, text, reply_markup=REPLY_MENU)
        else:
            await msg.reply_text(text, reply_markup=REPLY_MENU)
    except Exception as e:
        log.exception("download failed")
        text = f"❌ Ошибка: {_clean_err(e)}"
        if via_callback:
            await ctx.bot.send_message(chat_id, text, reply_markup=REPLY_MENU)
        else:
            await msg.reply_text(text, reply_markup=REPLY_MENU)
    finally:
        if path is not None:
            try:
                for f in path.parent.glob("*"):
                    f.unlink(missing_ok=True)
                path.parent.rmdir()
            except Exception:
                pass
        await _release_slot()


# ===== health-сервер (для Render) =====
def _start_health_server():
    port = int(os.getenv("PORT", "0"))
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK - bot is running")
        def log_message(self, *args):
            pass

    log.info("Health server on :%s", port)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


# ===== запуск =====
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",  "🏠 Меню"),
        BotCommand("help",   "ℹ️ Справка"),
        BotCommand("status", "📊 Статистика"),
        BotCommand("cancel", "❌ Отмена"),
    ])


def main():
    if not TOKEN:
        raise SystemExit("Не задан BOT_TOKEN.\n")

    if os.getenv("PORT"):
        Thread(target=_start_health_server, daemon=True).start()

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

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_button))

    log.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
