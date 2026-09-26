#!/usr/bin/env python3
"""One-time setup: sign into Google in *normal* Chrome using the dedicated
profile dir, so the session can be reused by analyze_worker.py.

We deliberately do NOT use Playwright here: Playwright launches Chrome with
automation flags, and Google answers that with "This browser or app may not be
secure" at the sign-in page. Launching Chrome yourself avoids that; the cookies
still land in the same .browser-profile/ that browser_session.py uses.
"""
import subprocess

from browser_session import PROFILE_DIR, find_chrome

GEMINI_URL = "https://gemini.google.com/app"


def main():
    PROFILE_DIR.mkdir(exist_ok=True)
    chrome = find_chrome()
    print(f"Opening normal Chrome with profile:\n  {PROFILE_DIR}\n")
    print("1. Sign into your Google account.")
    print("2. Confirm Gemini loads correctly.")
    print("3. Close that Chrome window, then come back here.\n")

    subprocess.Popen(
        [
            chrome,
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            GEMINI_URL,
        ]
    )
    input("Press Enter once you're logged in and Chrome is closed... ")
    print("Session saved to .browser-profile/. You can now run analyze_worker.py.")


if __name__ == "__main__":
    main()
