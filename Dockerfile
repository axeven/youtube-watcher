FROM python:3.12-slim

ARG TARGETARCH

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/watcher.db \
    VIDEO_CACHE_FILE=/data/video_cache.json \
    COOKIES_SRC=/cookies/yt-cookies.txt \
    YT_COOKIES_FILE=/tmp/yt-cookies.txt

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        unzip \
        tini \
        util-linux; \
    rm -rf /var/lib/apt/lists/*

# Deno is yt-dlp's JS runtime (used for YouTube signature/PO-token handling).
RUN set -eux; \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh; \
    deno --version

# supercronic: cron for containers (runs in the foreground, logs to stdout).
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64) sc_arch=amd64 ;; \
        arm64) sc_arch=arm64 ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-${sc_arch}"; \
    chmod +x /usr/local/bin/supercronic; \
    supercronic -version

WORKDIR /app

COPY requirements-docker.txt ./
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY db.py list_channel_videos.py list_channels.py web_app.py ./
COPY templates ./templates
COPY crontab entrypoint.sh ./
RUN chmod +x entrypoint.sh && mkdir -p /data

VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
