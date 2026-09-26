"""Persistent Chrome session so Google login survives across runs.

Google blocks sign-in from Playwright-launched browsers ("This browser or app
may not be secure") because Playwright starts Chrome with automation flags
(--enable-automation / navigator.webdriver). To avoid that we launch real
Chrome ourselves with the dedicated profile dir and attach over CDP - Chrome is
a normal, non-automated browser from Google's point of view.

Modes (env BROWSER_MODE):
    cdp         (default) launch normal Chrome + connect_over_cdp
    persistent  fall back to Playwright's launch_persistent_context

Log in once via login.py first.
"""
import atexit
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(__file__).parent / ".browser-profile"

CHROME_PATHS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/google-chrome-stable"),
]

CDP_PORT = int(os.environ.get("CHROME_CDP_PORT", "9222"))
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
MODE = os.environ.get("BROWSER_MODE", "cdp").lower()

STEALTH_INIT = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


def find_chrome() -> str:
    for path in CHROME_PATHS:
        if path.exists():
            return str(path)
    raise RuntimeError(
        "Google Chrome not found. Install it, or set BROWSER_MODE=persistent."
    )


def cdp_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def _wait_for_cdp(timeout: float = 25) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp_ready():
            return True
        time.sleep(0.5)
    return False


class BrowserSession:
    def __init__(self, headless: bool = False):
        PROFILE_DIR.mkdir(exist_ok=True)
        self._playwright = sync_playwright().start()
        self._chrome_proc = None
        if MODE == "persistent":
            self._start_persistent(headless)
        else:
            self._start_cdp()
        atexit.register(self.close)

    def _start_persistent(self, headless: bool) -> None:
        self.context = self._playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=headless,
            channel="chrome",
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def _start_cdp(self) -> None:
        if not cdp_ready():
            self._chrome_proc = subprocess.Popen(
                [
                    find_chrome(),
                    f"--user-data-dir={PROFILE_DIR}",
                    f"--remote-debugging-port={CDP_PORT}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if not _wait_for_cdp():
                raise RuntimeError("Chrome did not expose a CDP endpoint in time")

        self.browser = self._playwright.chromium.connect_over_cdp(CDP_URL)
        self.context = (
            self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        )
        try:
            self.context.add_init_script(STEALTH_INIT)
        except Exception:
            pass
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def close(self):
        if MODE == "persistent":
            try:
                self.context.close()
            except Exception:
                pass
        else:
            try:
                # Only close Chrome if this session launched it; leave a
                # pre-existing Chrome (and its warm session) alone.
                if self._chrome_proc is not None:
                    self.browser.close()
            except Exception:
                pass
        try:
            self._playwright.stop()
        except Exception:
            pass
