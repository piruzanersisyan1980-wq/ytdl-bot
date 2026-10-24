FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Системные пакеты + шрифты для водяного знака
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl unzip git fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Deno — JS-движок для n challenge YouTube
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt
RUN pip install --upgrade --force-reinstall "git+https://github.com/yt-dlp/yt-dlp.git@master"

# Предзагрузка скрипта-решателя JS-челленджей YouTube.
# Скачивание идёт один раз при сборке образа, поэтому в рантайме
# Render уже не будет пытаться тянуть его с GitHub заново.
# Если запустить не получится (нет cookies, блок IP и т.п.),
# сборка всё равно продолжится — отсюда `|| true`.
RUN yt-dlp --version \
 && python -c "import yt_dlp; \
o={'remote_components':['ejs:github'],'quiet':True,'no_warnings':True,'skip_download':True}; \
yt_dlp.YoutubeDL(o).extract_info('https://youtu.be/dQw4w9WgXcQ', download=False)" \
 || true

COPY bot.py .

CMD ["python", "bot.py"]
