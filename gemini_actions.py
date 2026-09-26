"""Deterministic Playwright actions for driving the Gemini web app - no LLM
in the loop. Selectors here were reverse-engineered against the actual
Gemini DOM (see project memory for how each was found).

Gemini is an Angular SPA: interacting before it finishes booting (or sending
the instant text lands) makes it hang on a spinner. So we wait for the app to
be ready and idle, then type and click with small randomized, human-like
delays.
"""
import random
import time

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

MODE_PICKER_SELECTOR = "[aria-label*='mode picker' i]"
CHAT_INPUT_SELECTOR = "[role='textbox']"
RESPONSE_SELECTOR = "message-content"
SEND_BUTTON_SELECTOR = (
    "button[aria-label*='Send' i], button[data-test-id*='send' i], "
    "button[aria-label*='Submit' i]"
)
# Only banner-style error surfaces, never the response body - a legitimate
# analysis could itself contain the words "something went wrong".
ERROR_SELECTOR = (
    "[role='alert'], mat-snack-bar-container, error-banner, "
    "[data-test-id='error-banner']"
)
# The Stop button only exists while Gemini is generating; it reverts to Send
# when the turn is complete - the authoritative "still working" signal.
STOP_BUTTON_SELECTOR = "button[aria-label*='Stop' i], button[data-test-id*='stop' i]"
BUSY_SELECTOR = STOP_BUTTON_SELECTOR

READY_TIMEOUT = 45
SUBMIT_CONFIRM_TIMEOUT = 20
# Let the SPA fully settle before typing - entering the URL too soon after the
# page/mode changes makes Gemini sit on a spinner.
PRE_SEND_DELAY = (5.0, 10.0)
# Consecutive unchanged 1s polls required before we trust the answer is final.
STABLE_POLLS_REQUIRED = 5


def _pause(page: Page, lo: float = 0.6, hi: float = 1.8) -> None:
    page.wait_for_timeout(random.uniform(lo, hi) * 1000)


def _is_busy(page: Page) -> bool:
    locator = page.locator(BUSY_SELECTOR)
    for i in range(locator.count()):
        try:
            if locator.nth(i).is_visible():
                return True
        except Exception:
            continue
    return False


def _wait_until_idle(page: Page, timeout: float = 60) -> None:
    # Wall clock, not monotonic: if the machine sleeps mid-run we want the
    # deadline to have passed on wake, not to be paused for the sleep duration.
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_busy(page):
            return
        page.wait_for_timeout(500)


def _visible_error(page: Page) -> str | None:
    """Return the text of a visible Gemini error banner, if one is showing."""
    locator = page.locator(ERROR_SELECTOR)
    for i in range(locator.count()):
        el = locator.nth(i)
        try:
            if el.is_visible():
                text = (el.inner_text() or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return None


def wait_for_ready(page: Page, timeout: float = READY_TIMEOUT) -> None:
    """Wait for the Gemini SPA to be booted and interactive before touching it."""
    page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
    box = page.locator(CHAT_INPUT_SELECTOR).first
    box.wait_for(state="visible", timeout=timeout * 1000)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if box.is_enabled() and not _is_busy(page):
            return
        page.wait_for_timeout(500)
    raise PlaywrightTimeoutError("Gemini input never became ready")


def ensure_pro_extended(page: Page) -> None:
    """Make sure the mode picker reads 'Pro' + 'Extended' before asking anything."""
    picker = page.locator(MODE_PICKER_SELECTOR).first
    picker.wait_for(state="visible", timeout=READY_TIMEOUT * 1000)

    label = picker.get_attribute("aria-label") or ""
    if "Pro" not in label:
        picker.click()
        _pause(page)
        page.locator("[role='menuitem']", has_text="3.1 Pro").first.click()
        _pause(page)

    label = picker.get_attribute("aria-label") or ""
    if "Extended" not in label:
        picker.click()
        _pause(page)
        page.locator("[role='menuitem']", has_text="Extended thinking").first.click()
        _pause(page)

    label = picker.get_attribute("aria-label") or ""
    if "Pro" not in label or "Extended" not in label:
        raise RuntimeError(f"Could not set mode to Pro Extended - picker now reads: {label!r}")


def _box_text(box) -> str:
    try:
        return box.input_value()
    except Exception:
        try:
            return box.inner_text()
        except Exception:
            return ""


def _click_send(page: Page) -> None:
    send = page.locator(SEND_BUTTON_SELECTOR).first
    try:
        if send.count():
            send.wait_for(state="visible", timeout=5000)
            deadline = time.time() + 10
            while time.time() < deadline:
                if send.is_enabled():
                    send.click()
                    return
                page.wait_for_timeout(200)
    except Exception:
        pass
    page.keyboard.press("Enter")


def send_prompt(page: Page, question: str) -> None:
    """Type the prompt and submit it, then confirm it actually left the input
    box - otherwise there's no point waiting for a response."""
    box = page.locator(CHAT_INPUT_SELECTOR).first
    box.click()
    _pause(page, 0.3, 0.9)

    try:
        box.fill("")  # clear any leftover text from a previous failed send
    except Exception:
        pass

    _pause(page, *PRE_SEND_DELAY)  # wait for the frontend to finish settling

    # Type instead of fill: the per-keystroke input events are what enable the
    # Send button in Gemini's Angular frontend.
    box.press_sequentially(question, delay=random.randint(20, 60))
    _pause(page, 0.8, 2.0)

    _wait_until_idle(page, timeout=30)  # don't queue behind an in-flight turn
    _click_send(page)

    deadline = time.time() + SUBMIT_CONFIRM_TIMEOUT
    while time.time() < deadline:
        err = _visible_error(page)
        if err:
            raise RuntimeError(f"Gemini rejected the prompt: {err}")
        if not _box_text(box).strip():
            return
        page.wait_for_timeout(300)

    # Nudge once more, then fail fast instead of waiting on a phantom response.
    page.keyboard.press("Enter")
    page.wait_for_timeout(1000)
    if not _box_text(box).strip():
        return
    raise RuntimeError("Prompt did not submit (input box still contains text)")


def wait_for_response(
    page: Page,
    baseline_count: int,
    question: str | None = None,
    first_timeout: float = 180,
    stream_timeout: float = 300,
) -> str:
    """Wait for a new message-content block to appear (bounded by
    first_timeout, aborting early on error banners), then wait for the answer
    to be complete: the Stop button gone AND the text unchanged for several
    seconds (bounded by stream_timeout)."""
    deadline = time.time() + first_timeout
    while time.time() < deadline:
        err = _visible_error(page)
        if err:
            raise RuntimeError(f"Gemini reported an error: {err}")
        if page.locator(RESPONSE_SELECTOR).count() > baseline_count:
            break
        page.wait_for_timeout(500)
    else:
        raise PlaywrightTimeoutError(
            f"Gemini produced no response within {first_timeout:.0f}s"
        )

    question_norm = (question or "").strip()
    last_text = None
    stable_polls = 0
    stream_deadline = time.time() + stream_timeout
    while time.time() < stream_deadline:
        err = _visible_error(page)
        if err:
            raise RuntimeError(f"Gemini reported an error mid-response: {err}")
        current = page.locator(RESPONSE_SELECTOR).last.inner_text()
        # Ignore the echoed prompt (if user turns also render as message-content).
        complete = (
            current
            and current.strip() != question_norm
            and current == last_text
            and not _is_busy(page)
        )
        if complete:
            stable_polls += 1
            if stable_polls >= STABLE_POLLS_REQUIRED:
                return current
        else:
            stable_polls = 0
        last_text = current
        page.wait_for_timeout(1000)

    return last_text or ""


def ask(
    page: Page, question: str, first_timeout: float = 180, stream_timeout: float = 300
) -> str:
    wait_for_ready(page)
    ensure_pro_extended(page)
    baseline_count = page.locator(RESPONSE_SELECTOR).count()
    send_prompt(page, question)
    return wait_for_response(
        page,
        baseline_count,
        question=question,
        first_timeout=first_timeout,
        stream_timeout=stream_timeout,
    )
