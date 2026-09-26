#!/usr/bin/env python3
"""Ask the "Investment Video Analyzer V2" Gemini gem a question via
deterministic Playwright automation (no LLM in the loop). Saves each result
to its own file under results/, named with a UTC+7 timestamp prefix.

Usage:
    python analyze_investment_video.py "your question or video URL here"
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from browser_session import BrowserSession
from gemini_actions import ask

# "Investment Video Analyzer V2" gem
GEMINI_URL = "https://gemini.google.com/gem/bf9afb989f09"

RESULTS_DIR = Path(__file__).parent / "results"
UTC_PLUS_7 = timezone(timedelta(hours=7))

YOUTUBE_ID_RE = re.compile(r"(?:youtu\.be/|v=)([A-Za-z0-9_-]{11})")


def slug_for(question: str) -> str:
    match = YOUTUBE_ID_RE.search(question)
    if match:
        return match.group(1)
    return re.sub(r"[^A-Za-z0-9]+", "_", question).strip("_")[:50] or "result"


def result_filename(question: str) -> str:
    timestamp = datetime.now(UTC_PLUS_7).strftime("%Y-%m-%d_%H%M%S")
    return f"{timestamp}_{slug_for(question)}.txt"


def find_existing_result(question: str) -> Path | None:
    matches = sorted(RESULTS_DIR.glob(f"*_{slug_for(question)}.txt"))
    return matches[-1] if matches else None


def main():
    if len(sys.argv) < 2:
        print('Usage: python analyze_investment_video.py "your question"', file=sys.stderr)
        sys.exit(1)

    question = sys.argv[1]

    existing = find_existing_result(question)
    if existing:
        print(f"Already asked, skipping - see {existing}", file=sys.stderr)
        _, _, answer = existing.read_text(encoding="utf-8").partition("\n\n")
        print(answer.strip())
        return

    session = BrowserSession(headless=False)

    try:
        session.page.goto(GEMINI_URL, wait_until="domcontentloaded")
        session.page.wait_for_timeout(2000)
        answer = ask(session.page, question)
        print(answer)

        RESULTS_DIR.mkdir(exist_ok=True)
        out_path = RESULTS_DIR / result_filename(question)
        out_path.write_text(
            f"Question: {question}\n"
            f"Timestamp (UTC+7): {datetime.now(UTC_PLUS_7).isoformat()}\n\n"
            f"{answer}\n",
            encoding="utf-8",
        )
        print(f"Saved to {out_path}", file=sys.stderr)
    finally:
        session.close()


if __name__ == "__main__":
    main()
