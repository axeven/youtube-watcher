#!/bin/sh
set -eu

export PYTHONUNBUFFERED=1

# yt-dlp rewrites the cookie file (rotated cookies), so the host file is
# mounted read-only and copied to a writable path at startup.
if [ -f "${COOKIES_SRC:-/cookies/yt-cookies.txt}" ]; then
  cp "${COOKIES_SRC:-/cookies/yt-cookies.txt}" "${YT_COOKIES_FILE:-/tmp/yt-cookies.txt}"
  echo "Loaded YouTube cookies from ${COOKIES_SRC:-/cookies/yt-cookies.txt}"
else
  echo "WARNING: no cookie file at ${COOKIES_SRC:-/cookies/yt-cookies.txt}; scraping anonymously" >&2
fi

python -c "import db; db.init_db()"

# Scheduled scrape runner (supercronic, foreground-friendly, logs to stdout).
supercronic /app/crontab &

# One immediate scrape in the background so the viewer has data right away.
# flock keeps this from colliding with a cron tick.
flock -n /tmp/scrape.lock python /app/list_channels.py --quiet &

# Web viewer is PID 1's child (tini reaps the background jobs above).
exec gunicorn \
  --workers 1 \
  --threads 4 \
  --bind 0.0.0.0:8000 \
  --access-logfile - \
  --error-logfile - \
  web_app:app
