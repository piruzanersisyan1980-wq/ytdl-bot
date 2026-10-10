FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl unzip git \
    && rm -rf /var/lib/apt/lists/*

# Deno — для YouTube JS-челленджей
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# yt-dlp из GitHub master — всегда самая свежая версия
RUN pip install --upgrade --force-reinstall "git+https://github.com/yt-dlp/yt-dlp.git@master"

# Показать версию в логах при сборке
RUN yt-dlp --version

COPY bot.py .

# Авто-обновление yt-dlp при каждом запуске контейнера
CMD ["sh", "-c", "pip install -U --force-reinstall 'git+https://github.com/yt-dlp/yt-dlp.git@master' --quiet && exec python bot.py"]
