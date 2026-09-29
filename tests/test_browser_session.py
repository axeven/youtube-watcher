"""Guard the Chrome launch flags - no browser is started, no network.

Run: ./venv/bin/python tests/test_browser_session.py

Stubs find_chrome (so a machine without Chrome still tests the argv) and the
version probe, then asserts what the worker's default launch actually contains:
headless on, and the normal windowed user agent instead of HeadlessChrome's.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("REPO_DIR") or (os.path.dirname(HERE) if os.path.basename(HERE) == "tests" else HERE)
sys.path.insert(0, REPO)

import browser_session as bs  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


bs.find_chrome = lambda: "/usr/bin/google-chrome-stable"  # type: ignore[assignment]
bs._chrome_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"

headless_argv = bs.chrome_argv(True)
headful_argv = bs.chrome_argv(False)

check("headless adds exactly one headless flag",
      sum(1 for a in headless_argv if a.startswith("--headless")) == 1,
      [a for a in headless_argv if a.startswith("--headless")])
check("headless flag defaults to --headless=new", bs.HEADLESS_FLAG in headless_argv, bs.HEADLESS_FLAG)
check("headless sends the windowed UA",
      any(a.startswith("--user-agent=") and "HeadlessChrome" not in a for a in headless_argv))
check("headful does not force a UA or headless flag",
      not any(a.startswith("--headless") or a.startswith("--user-agent=") for a in headful_argv),
      headful_argv)
check("both keep the dedicated profile and CDP port",
      all(f"--user-data-dir={bs.PROFILE_DIR}" in a and any(x.startswith("--remote-debugging-port=") for x in a)
          for a in (headless_argv, headful_argv)))
check("about:blank is last", headless_argv[-1] == "about:blank" and headful_argv[-1] == "about:blank")

# The UA is derived from the installed Chrome's major version, not hardcoded.
def _fake_run(stdout):
    def run(*args, **kwargs):
        return type("R", (), {"stdout": stdout})()

    return run


bs._chrome_ua = None
real_run = bs.subprocess.run
try:
    bs.subprocess.run = _fake_run("Google Chrome 154.0.8037.57\n")
    check("UA version tracks the installed Chrome",
          bs.chrome_user_agent() == (
              "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
          ), bs.chrome_user_agent())
    bs._chrome_ua = None

    def _boom(*args, **kwargs):
        raise FileNotFoundError("chrome.exe")

    bs.subprocess.run = _boom
    check("unparsable version → no UA override (chrome.exe --version is silent on Windows)",
          bs.chrome_user_agent() is None)
finally:
    bs.subprocess.run = real_run
    bs._chrome_ua = ""

# Env switch: headless is the default, 0/false/no/off opt out.
for value, expected in (("", True), ("1", True), ("0", False), ("false", False), ("no", False), ("off", False)):
    if value:
        os.environ["BROWSER_HEADLESS"] = value
    else:
        os.environ.pop("BROWSER_HEADLESS", None)
    check(f"BROWSER_HEADLESS={value or '<unset>'} → headless={expected}",
          bs.headless_default() is expected, bs.headless_default())
os.environ.pop("BROWSER_HEADLESS", None)

print()
width = max(len(n) for n, _, _ in results)
failed = 0
for name, ok, detail in results:
    if not ok:
        failed += 1
        print(f"FAIL  {name:<{width}}  {detail}")
    else:
        print(f"ok    {name}")
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
