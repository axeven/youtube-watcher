#!/usr/bin/env python3
"""Hourly worker: claim one scraped video from the Docker analysis API, analyze
it with the "Investment Video Analyzer V2" Gemini gem using the local Chrome
session, then post the answer back.

Run once per invocation (Windows Task Scheduler drives the hourly cadence):
    python analyze_worker.py

Config (env, or a .env file next to this script):
    WATCHER_API            base URL of the web viewer        (default http://localhost:8000)
    ANALYSIS_TOKEN         shared secret matching the server (default: from .env)
    ANALYZE_FIRST_TIMEOUT  seconds to wait for a reply to start (default 180)
    ANALYZE_TIMEOUT        seconds to wait for streaming to finish (default 300)
    ANALYSIS_MIN_WORDS     answers shorter than this are soft failures (default 50)
    ANALYSIS_ATTEMPTS      immediate retries when an answer is short/empty (default 2)
    BROWSER_HEADLESS       0 opens a real Chrome window instead of headless (default 1)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analyze_investment_video import (
    GEMINI_URL,
    RESULTS_DIR,
    find_existing_result,
    result_filename,
)
from browser_session import BrowserSession
from gemini_actions import ask

HERE = Path(__file__).parent
LOCK_FILE = HERE / ".analyze_worker.lock"
UTC_PLUS_7 = timezone(timedelta(hours=7))

API_BASE = os.environ.get("WATCHER_API", "http://localhost:8000").rstrip("/")
FIRST_TIMEOUT = float(os.environ.get("ANALYZE_FIRST_TIMEOUT", "180"))
STREAM_TIMEOUT = float(os.environ.get("ANALYZE_TIMEOUT", "300"))
MIN_ANSWER_WORDS = int(os.environ.get("ANALYSIS_MIN_WORDS", "50"))
ASK_ATTEMPTS = int(os.environ.get("ANALYSIS_ATTEMPTS", "2"))
# A run that dies leaves its lock behind; steal it after this long.
LOCK_STALE_SECONDS = 45 * 60


def load_dotenv() -> None:
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def api_request(path: str, method: str = "GET", payload: dict | None = None):
    """Return (status_code, parsed_json_or_None). Never raises on HTTP errors."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API_BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    token = os.environ.get("ANALYSIS_TOKEN", "")
    if token:
        req.add_header("X-Analysis-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return e.code, (json.loads(body) if body else None)
    except urllib.error.URLError as e:
        return 0, {"error": str(e)}


def acquire_lock() -> bool:
    """Single-instance guard so an overrunning run can't open a second browser."""
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        if time.time() - LOCK_FILE.stat().st_mtime > LOCK_STALE_SECONDS:
            LOCK_FILE.unlink(missing_ok=True)
            return acquire_lock()
        return False


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def submit(video_id: str, answer: str | None = None, error: str | None = None) -> bool:
    status, body = api_request(
        f"/api/analyses/{video_id}",
        method="POST",
        payload={"answer": answer, "error": error},
    )
    if status != 200:
        print(f"Failed to submit result: HTTP {status} {body}", file=sys.stderr)
        return False
    return True


def save_result(url: str, answer: str) -> Path:
    if not answer or not answer.strip():
        raise ValueError("refusing to save an empty answer")
    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / result_filename(url)
    out_path.write_text(
        f"Question: {url}\n"
        f"Timestamp (UTC+7): {datetime.now(UTC_PLUS_7).isoformat()}\n\n"
        f"{answer}\n",
        encoding="utf-8",
    )
    return out_path


def cached_answer(url: str) -> str | None:
    """Answer text from an existing results file, or None if absent/empty/too
    short. Empty or short files are soft failures and must not be reused."""
    existing = find_existing_result(url)
    if not existing:
        return None
    _, _, answer = existing.read_text(encoding="utf-8").partition("\n\n")
    answer = answer.strip()
    if len(answer.split()) < MIN_ANSWER_WORDS:
        return None
    return answer


def _ask_once(url: str) -> str:
    # Windowless by default; BROWSER_HEADLESS=0 opens a real window (see
    # browser_session).
    session = BrowserSession()
    try:
        session.page.goto(GEMINI_URL, wait_until="domcontentloaded")
        session.page.wait_for_timeout(2000)
        return ask(
            session.page, url, first_timeout=FIRST_TIMEOUT, stream_timeout=STREAM_TIMEOUT
        )
    finally:
        session.close()


def run_analysis(url: str) -> str:
    """Ask Gemini, retrying immediately on a soft failure (empty/short answer).
    Hard failures (timeouts, browser errors) propagate and are retried by the
    hourly schedule via the DB instead."""
    last_error = "no attempt made"
    for attempt in range(1, ASK_ATTEMPTS + 1):
        answer = _ask_once(url)
        words = len(answer.split())
        if words >= MIN_ANSWER_WORDS:
            return answer
        last_error = (
            f"Answer too short ({words} words < {MIN_ANSWER_WORDS}) - likely a soft failure"
            if answer.strip()
            else "Gemini returned an empty response"
        )
        print(
            f"Attempt {attempt}/{ASK_ATTEMPTS}: {last_error}; retrying",
            file=sys.stderr,
        )
        time.sleep(5)
    raise RuntimeError(last_error)


def main() -> int:
    load_dotenv()

    if not acquire_lock():
        print("Another analysis run is active; skipping.", file=sys.stderr)
        return 0

    try:
        # We hold the lock, so no other worker is active: any 'running' row is
        # left over from a run that was killed. Recover it so it can be retried.
        recover_status, recover_body = api_request(
            "/api/analyses/abandon-running", method="POST"
        )
        if recover_status == 200 and recover_body and recover_body.get("abandoned"):
            print(f"Recovered {recover_body['abandoned']} abandoned running analysis(es)")

        status, claim = api_request("/api/analyses/claim", method="POST")
        if status == 401:
            print("Unauthorized - check ANALYSIS_TOKEN matches the server.", file=sys.stderr)
            return 1
        if status == 0:
            print(f"Cannot reach {API_BASE}: {claim}", file=sys.stderr)
            return 1
        if status == 204 or not claim:
            print("Nothing to analyze.", file=sys.stderr)
            return 0

        video_id = claim["video_id"]
        url = claim["url"]
        print(f"Claimed {video_id} (attempt {claim.get('attempts')}): {claim.get('title', '')}")

        # A retry (invalid/error/short/stuck) must be re-analyzed, not served
        # from a cached results file.
        existing_answer = None if claim.get("retry") else cached_answer(url)
        if existing_answer:
            ok = submit(video_id, answer=existing_answer)
            print(f"Reused existing result for {video_id}" if ok else "Submit failed.")
            return 0 if ok else 1

        try:
            answer = run_analysis(url)
        except Exception as e:  # noqa: BLE001 - report any failure back to the API
            submit(video_id, error=f"{type(e).__name__}: {e}")
            print(f"Analysis failed: {e}", file=sys.stderr)
            return 1

        out_path = save_result(url, answer)
        if not submit(video_id, answer=answer):
            return 1
        print(f"Done {video_id} ({len(answer.split())} words) -> {out_path}")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
