# bot.py
import os
import re
import time
import asyncio
import logging
import tempfile
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
COOKIES_FILE   = Path(__file__).parent / "cookies.txt"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ytdl-bot")

URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)"
    r"[\w\-]{6,}"
)
# =====================

# ===== СЧЁТЧИКИ ЗАДАЧ =====
_semaphore  = asyncio.Semaphore(MAX_CONCURRENT)
_stats_lock = asyncio.Lock()
_active     = 0
_waiting    = 0
_finished   = 0
_started_at = time.time()
# ==========================

# ===== КЛАВИАТУРЫ =====
REPLY_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🎬 Скачать видео")],
        [KeyboardButton("🎵 Скачать MP3"), KeyboardButton("ℹ️ Помощь")],
        [KeyboardButton("📊 Статус"),      KeyboardButton("❌ Отмена")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Вставьте ссылку на YouTube…",
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
        await query.edit_message_text(text)
        return
    except Exception:
        pass
    try:
        await query.edit_message_caption(caption=text)
        return
    except Exception:
        pass
    await query.message.reply_text(text)


async def _clear_keyboard(query):
    try:
        await query.edit_message_reply_markup(reply_markup=EMPTY_KB)
    except Exception:
        pass


# ===== очередь =====
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
    cookies_note = "🍪 Cookies: загружены" if COOKIES_FILE.exists() else "⚠️ Cookies: НЕТ (нужны для YouTube)"
    await update.message.reply_text(
        "👋 Привет! Я качаю видео с YouTube.\n\n"
        "Просто **пришлите ссылку** — я покажу превью и кнопки качества.\n\n"
        f"⚠️ Лимит файла — {MAX_SIZE_MB} МБ.\n"
        f"⚙️ Одновременно обрабатывается до {MAX_CONCURRENT} задач.\n"
        f"{cookies_note}",
        parse_mode="Markdown",
        reply_markup=REPLY_MENU,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Справка**\n\n"
        "• Кинуть ссылку из YouTube / Shorts / youtu.be.\n"
        "• Бот покажет превью и кнопки: 1080p / 720p / 480p / 360p / MP3.\n"
        "• **⬅️ Назад** — ввести другую ссылку.\n\n"
        "Команды:\n"
        "/start — меню\n"
        "/video <ссылка> — сразу 720p\n"
        "/audio <ссылка> — сразу MP3\n"
        "/status — статистика бота\n"
        "/cancel — отменить ввод",
        parse_mode="Markdown",
        reply_markup=REPLY_MENU,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("url", None)
    ctx.user_data.pop("mode", None)
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


async def cmd_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _quick(update, ctx, "720")


async def cmd_audio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _quick(update, ctx, "audio")


async def _quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE, kind: str):
    m = URL_RE.search(update.message.text or "")
    if not m:
        await update.message.reply_text(
            "Пришлите ссылку вместе с командой:\n`/audio https://youtu.be/…`",
            parse_mode="Markdown",
            reply_markup=REPLY_MENU,
        )
        return
    await _do_download(update, ctx, m.group(0), kind)


# ===== кнопки меню =====
async def handle_menu_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "🎬 Скачать видео":
        ctx.user_data["mode"] = "video"
        await update.message.reply_text("🎬 Пришлите ссылку на видео.", reply_markup=REPLY_MENU)
        return
    if text == "🎵 Скачать MP3":
        ctx.user_data["mode"] = "audio"
        await update.message.reply_text("🎵 Пришлите ссылку — верну MP3.", reply_markup=REPLY_MENU)
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

    await handle_link(update, ctx)


# ===== обработка ссылки + превью =====
async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text(
            "🤔 Это не похоже на ссылку YouTube.\n"
            "Пришлите ссылку вида `https://youtu.be/…`",
            parse_mode="Markdown",
            reply_markup=REPLY_MENU,
        )
        return

    url = m.group(0)
    ctx.user_data["url"] = url

    mode = ctx.user_data.pop("mode", None)
    if mode == "audio":
        await _do_download(update, ctx, url, "audio")
        return

    placeholder = await update.message.reply_text("🔍 Получаю информацию…")
    await ctx.bot.send_chat_action(update.message.chat_id, ChatAction.TYPING)

    try:
        info = await asyncio.get_running_loop().run_in_executor(None, _extract_info_sync, url)
    except Exception as e:
        err = _clean_err(e)
        await placeholder.edit_text(f"❌ Не удалось получить видео:\n{err}")
        return

    title    = _truncate(info.get("title") or "Видео", MAX_TITLE_LEN)
    uploader = _truncate(info.get("uploader") or "", 40)
    dur      = _fmt_duration(info.get("duration"))
    thumb    = info.get("thumbnail")

    lines = [f"🎬 {title}"]
    if uploader: lines.append(f"👤 {uploader}")
    if dur:      lines.append(f"⏱ {dur}")
    lines.append("")
    lines.append("Выберите качество:")
    caption = "\n".join(lines)

    await placeholder.delete()
    if thumb:
        try:
            await update.message.reply_photo(
                photo=thumb, caption=caption, reply_markup=QUALITY_MENU,
            )
            return
        except Exception as e:
            log.warning("Не отправить превью: %s", e)

    await update.message.reply_text(caption, reply_markup=QUALITY_MENU)


# ===== инлайн-кнопки =====
async def on_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    kind = query.data.split(":", 1)[1]

    if kind == "back":
        ctx.user_data.pop("url", None)
        ctx.user_data.pop("mode", None)
        try:
            await query.edit_message_caption(caption="⬅️ Ок. Пришлите новую ссылку.", reply_markup=EMPTY_KB)
            return
        except Exception:
            pass
        await _set_status(query, "⬅️ Ок. Пришлите новую ссылку.")
        await _clear_keyboard(query)
        return

    if kind == "cancel":
        ctx.user_data.pop("url", None)
        ctx.user_data.pop("mode", None)
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


# ===== скачивание =====
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
            text = (
                f"❌ Файл {size_mb:.1f} МБ — больше лимита Telegram ({MAX_SIZE_MB} МБ).\n"
                f"Попробуйте качество поменьше или MP3."
            )
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
        text = f"❌ yt-dlp не смог скачать:\n{_clean_err(e)}"
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
        BotCommand("video",  "🎬 Скачать видео (720p)"),
        BotCommand("audio",  "🎵 Скачать MP3"),
        BotCommand("status", "📊 Статистика"),
        BotCommand("help",   "ℹ️ Справка"),
        BotCommand("cancel", "❌ Отмена"),
    ])


def main():
    if not TOKEN:
        raise SystemExit(
            "Не задан BOT_TOKEN.\n"
            'PowerShell: $env:BOT_TOKEN = "123456:ABC..."\n'
        )

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
    app.add_handler(CommandHandler("video",  cmd_video))
    app.add_handler(CommandHandler("audio",  cmd_audio))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^q:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_button))

    log.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()