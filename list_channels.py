#!/usr/bin/env python3
"""List recent public videos/lives for multiple YouTube channels at once,
fetched concurrently, and persist them to the SQLite DB used by the web viewer.

Usage:
    python list_channels.py [--quiet]
        (reads channels.txt in this directory, one channel URL per line)

    python list_channels.py [--quiet] "https://www.youtube.com/@Bennix/videos" ...
        (explicit URLs override channels.txt)

--quiet suppresses the human-readable listing (used by the hourly cron job);
scrape progress/errors still go to stderr.
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import db
from list_channel_videos import format_result_line, list_recent

COUNT = 20
CHANNELS_FILE = Path(__file__).parent / "channels.txt"
# Too much concurrency (one thread per channel, all hammering YouTube at
# once) trips "Sign in to confirm you're not a bot" - keep this modest.
MAX_WORKERS = 3


def read_channels_file() -> list[str]:
    if not CHANNELS_FILE.exists():
        return []
    lines = CHANNELS_FILE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def channel_display_name(channel_url: str) -> str:
    """Derive a friendly name from the URL handle (e.g. @Bennix)."""
    handle = channel_url.rstrip("/")
    for suffix in ("/videos", "/streams"):
        handle = handle.removesuffix(suffix)
    handle = handle.rstrip("/").rsplit("/", 1)[-1]
    return handle.lstrip("@") or channel_url


def scrape_channel(url: str) -> tuple[str, list[dict] | Exception]:
    try:
        return url, list_recent(url, COUNT)
    except Exception as e:
        return url, e


def run_scrape(channel_urls: list[str], quiet: bool = False) -> int:
    """Fetch every channel, write results to the DB, and record a scrape run.
    Returns a process exit code (0 unless every channel failed)."""
    db.init_db()
    run_id = db.record_scrape_start()

    results_by_url = {}
    with ThreadPoolExecutor(max_workers=min(len(channel_urls), MAX_WORKERS)) as pool:
        futures = {pool.submit(scrape_channel, url): url for url in channel_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                _, result = future.result()
                results_by_url[url] = result
            except Exception as e:
                results_by_url[url] = e

    channels_ok = 0
    channels_failed = 0
    videos_upserted = 0

    for url in channel_urls:
        result = results_by_url.get(url)
        if isinstance(result, Exception):
            channels_failed += 1
            print(f"ERROR {url}: {result}", file=sys.stderr)
            continue

        try:
            channel_id = db.upsert_channel(url, channel_display_name(url))
            videos_upserted += db.upsert_videos(channel_id, result)
            db.mark_channel_scraped(channel_id)
            channels_ok += 1
        except Exception as e:
            channels_failed += 1
            print(f"ERROR storing {url}: {e}", file=sys.stderr)
            continue

        if not quiet:
            print(f"=== {url} ===")
            for i, r in enumerate(result, 1):
                print(format_result_line(i, r))
            print()

    status = "ok" if channels_failed == 0 else ("error" if channels_ok == 0 else "partial")
    db.record_scrape_finish(run_id, status, channels_ok, channels_failed, videos_upserted)
    print(
        f"Scrape {status}: {channels_ok} ok, {channels_failed} failed, "
        f"{videos_upserted} videos stored",
        file=sys.stderr,
    )
    return 1 if channels_ok == 0 else 0


def main():
    args = sys.argv[1:]
    quiet = "--quiet" in args
    args = [a for a in args if a != "--quiet"]

    channel_urls = args or read_channels_file()
    if not channel_urls:
        print(
            'Usage: python list_channels.py [--quiet] ["channel URL" ...] '
            "(or list them in channels.txt)",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.exit(run_scrape(channel_urls, quiet=quiet))


if __name__ == "__main__":
    main()
