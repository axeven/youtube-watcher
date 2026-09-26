#!/usr/bin/env python3
"""List a YouTube channel's most recent public (non-member-only) videos and
live streams, combined and sorted by upload date. Uses yt-dlp - free, no API
key or quota needed.

Usage:
    python list_channel_videos.py "https://www.youtube.com/@Bennix" [count]
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

# yt-dlp's default JS runtime (needed for YouTube's anti-bot/PO token
# handling) - installed via `winget install DenoLand.Deno`, not on PATH
# by default, so add it here regardless of the invoking shell's env.
DENO_DIR = Path(
    r"C:\Users\lathi\AppData\Local\Microsoft\WinGet\Packages"
    r"\DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe"
)
if DENO_DIR.exists():
    os.environ["PATH"] = str(DENO_DIR) + os.pathsep + os.environ.get("PATH", "")

import yt_dlp


class _NoSaveYoutubeDL(yt_dlp.YoutubeDL):
    """yt-dlp rewrites the cookie file on close (save_cookies). With several
    concurrent scrapes sharing one cookie file that clobbers the file mid-read,
    causing spurious "does not look like a Netscape format cookies file"
    errors. We only ever read cookies, so skip the write-back entirely."""

    def save_cookies(self):
        pass


# How many flat entries to pull from each tab before filtering - buffer so
# filtering out member-only content still leaves enough candidates. Each
# candidate not already in the cache costs one real request, so keep modest.
FLAT_FETCH_LIMIT = 20

# Seconds to wait between per-video enrichment requests (only applies to
# cache misses - cached videos don't need a request at all).
REQUEST_DELAY = 1.0

PUBLIC_AVAILABILITY = {None, "public"}

# Per-video metadata (title/timestamp/duration) never changes once published,
# so cache it permanently by video id - reruns only pay the request cost for
# videos not seen before. In Docker this is pointed at the persistent volume so
# the cache survives container re-creation.
CACHE_FILE = Path(
    os.environ.get("VIDEO_CACHE_FILE", str(Path(__file__).parent / ".video_cache.json"))
)
_cache_lock = threading.Lock()
_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    with _cache_lock:
        if _cache is None:
            if CACHE_FILE.exists():
                _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            else:
                _cache = {}
        return _cache


def _save_cache():
    with _cache_lock:
        CACHE_FILE.write_text(json.dumps(_cache, indent=2), encoding="utf-8")

# Reuse the Gemini project's dedicated (non-live-profile) logged-in Chrome
# profile so yt-dlp can authenticate as a real signed-in user - anonymous
# requests get hit with "Sign in to confirm you're not a bot". Cookies are
# exported to a static file once (rather than read from the browser DB on
# every request, which is slower and not needed once exported).
BROWSER_PROFILE_DIR = Path(__file__).parent / ".browser-profile"
# In Docker the cookie file is mounted in; YT_COOKIES_FILE lets the container
# point at a different path than the host export.
COOKIE_FILE = Path(
    os.environ.get("YT_COOKIES_FILE", str(Path(__file__).parent / ".yt-cookies.txt"))
)
_cookie_file_lock = threading.Lock()


class _QuietLogger:
    """Swallow yt-dlp's own logging - expected conditions (e.g. a channel with
    no /streams tab) are handled by callers, and real failures surface as
    exceptions that list_channels.py reports."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg, only_once=False):
        pass

    def error(self, msg):
        pass


_QUIET_LOGGER = _QuietLogger()


def ensure_cookie_file() -> str:
    with _cookie_file_lock:
        if not COOKIE_FILE.exists():
            import yt_dlp.cookies

            jar = yt_dlp.cookies.extract_cookies_from_browser(
                "chrome", str(BROWSER_PROFILE_DIR), _QUIET_LOGGER
            )
            jar.save(str(COOKIE_FILE), ignore_discard=True, ignore_expires=True)
    return str(COOKIE_FILE)


def channel_tab_url(channel_url: str, tab: str) -> str:
    return channel_url.rstrip("/").removesuffix("/videos").removesuffix("/streams") + f"/{tab}"


def fetch_flat_candidates(channel_url: str, tab: str, kind: str) -> list[dict]:
    """Fast pass: list entries and their availability, without per-video detail."""
    opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "playlistend": FLAT_FETCH_LIMIT,
        "cookiefile": ensure_cookie_file(),
        "logger": _QUIET_LOGGER,
    }
    try:
        with _NoSaveYoutubeDL(opts) as ydl:
            info = ydl.extract_info(channel_tab_url(channel_url, tab), download=False)
    except yt_dlp.utils.DownloadError as e:
        if "does not have a" in str(e) and "tab" in str(e):
            return []  # channel has no /streams (or /videos) tab at all
        raise
    entries = info.get("entries") or []
    candidates = []
    for e in entries:
        if e.get("availability") not in PUBLIC_AVAILABILITY:
            continue
        candidates.append({"id": e["id"], "url": e["url"], "kind": kind})
    return candidates


def enrich_with_timestamp(candidate: dict, retries: int = 3) -> dict | None:
    """Slow pass per candidate: real upload timestamp + a final availability
    re-check (the flat pass can miss some restriction types)."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "cookiefile": ensure_cookie_file(),
        "logger": _QUIET_LOGGER,
    }
    for attempt in range(retries):
        try:
            with _NoSaveYoutubeDL(opts) as ydl:
                info = ydl.extract_info(candidate["url"], download=False, process=False)
            break
        except yt_dlp.utils.DownloadError:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))  # transient YouTube hiccup - back off and retry
    if info.get("availability") not in PUBLIC_AVAILABILITY:
        return None
    return {
        **candidate,
        "title": info.get("title", ""),
        "timestamp": info.get("timestamp") or 0,
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
    }


def list_recent(channel_url: str, count: int = 20) -> list[dict]:
    flat_candidates = fetch_flat_candidates(channel_url, "videos", "video") + fetch_flat_candidates(
        channel_url, "streams", "live"
    )

    cache = _load_cache()
    enriched = []
    made_request = False
    for c in flat_candidates:
        cached = cache.get(c["id"])
        if cached:
            enriched.append({**c, **cached})
            continue

        if made_request:
            time.sleep(REQUEST_DELAY)  # avoid bursty request rate that trips YouTube's bot check
        made_request = True
        try:
            result = enrich_with_timestamp(c)
        except yt_dlp.utils.DownloadError as e:
            print(f"Skipping {c['url']} - extraction failed: {e}", file=sys.stderr)
            continue
        if result:
            enriched.append(result)
            cache[c["id"]] = {
                "title": result["title"],
                "timestamp": result["timestamp"],
                "upload_date": result["upload_date"],
                "duration": result["duration"],
            }
            _save_cache()  # persist incrementally so progress survives interruptions

    enriched.sort(key=lambda x: x["timestamp"], reverse=True)
    return enriched[:count]


def format_duration(seconds) -> str:
    if not seconds:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_result_line(i: int, r: dict) -> str:
    title = r["title"].encode("ascii", errors="replace").decode("ascii")
    return (
        f"{i:2}. [{r['kind']:5}] {r['upload_date']} ({format_duration(r['duration'])}) "
        f"{title}\n     {r['url']}"
    )


def main():
    if len(sys.argv) < 2:
        print('Usage: python list_channel_videos.py "channel URL" [count]', file=sys.stderr)
        sys.exit(1)

    channel_url = sys.argv[1]
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    results = list_recent(channel_url, count)

    for i, r in enumerate(results, 1):
        print(format_result_line(i, r))


if __name__ == "__main__":
    main()
