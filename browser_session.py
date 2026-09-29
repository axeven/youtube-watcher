"""Persistent Chrome session so Google login survives across runs.

Google blocks sign-in from Playwright-launched browsers ("This browser or app
may not be secure") because Playwright starts Chrome with automation flags
(--enable-automation / navigator.webdriver). To avoid that we launch real
Chrome ourselves with the dedicated profile dir and attach over CDP - Chrome is
a normal, non-automated browser from Google's point of view.

Modes (env BROWSER_MODE):
    cdp         (default) launch normal Chrome + connect_over_cdp
    persistent  fall back to Playwright's launch_persistent_context

Headless (env BROWSER_HEADLESS, on by default): the Gemini gem renders the same
windowless (verified: the same signed-in account, the same "Pro Extended" mode
picker, the same prompt box), so the worker never pops a window onto WSLg and
the systemd unit stops depending on DISPLAY. BROWSER_HEADLESS=0 restores the
visible window when debugging.

Log in once via login.py first (always headful - signing in by hand needs a
window).
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

# Chrome 132+ folded the two headless modes into one; --headless=new remains
# accepted as its alias, which keeps this working on older builds too.
HEADLESS_FLAG = os.environ.get("CHROME_HEADLESS_FLAG", "--headless=new")

_chrome_ua: str | None = None


def headless_default() -> bool:
    """Windowless unless BROWSER_HEADLESS says otherwise (0/false/no/off)."""
    return os.environ.get("BROWSER_HEADLESS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def find_chrome() -> str:
    for path in CHROME_PATHS:
        if path.exists():
            return str(path)
    raise RuntimeError(
        "Google Chrome not found. Install it, or set BROWSER_MODE=persistent."
    )


def chrome_user_agent() -> str | None:
    """The UA a *windowed* Chrome of this build sends, or None to leave Chrome's
    own UA alone.

    Headless Chrome puts "HeadlessChrome/<version>" in the UA string - the one
    part of the session that looks unlike a normal browser, on an account whose
    login is the entire reason this module exists. The version probe is
    `--version`, which only prints on Linux (on Windows chrome.exe opens a
    window instead), so an unknown version means no override.
    """
    global _chrome_ua
    if _chrome_ua is None:
        try:
            out = subprocess.run(
                [find_chrome(), "--version"], capture_output=True, text=True, timeout=10
            ).stdout
            major = out.strip().rsplit(" ", 1)[-1].split(".")[0]
            assert major.isdigit()
            _chrome_ua = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
            )
        except Exception:
            _chrome_ua = ""
    return _chrome_ua or None


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


def chrome_argv(headless: bool) -> list[str]:
    """Chrome's command line, split out so it can be asserted without launching."""
    argv = [
        find_chrome(),
        f"--user-data-dir={PROFILE_DIR}",
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        argv.append(HEADLESS_FLAG)
        ua = chrome_user_agent()
        if ua:
            argv.append(f"--user-agent={ua}")
    return [*argv, "about:blank"]


class BrowserSession:
    def __init__(self, headless: bool | None = None):
        PROFILE_DIR.mkdir(exist_ok=True)
        self.headless = headless_default() if headless is None else headless
        self._playwright = sync_playwright().start()
        self._chrome_proc = None
        if MODE == "persistent":
            self._start_persistent(self.headless)
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
                chrome_argv(self.headless),
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
