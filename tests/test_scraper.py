"""Guard the scraper's selection rules - no YouTube requests, no network.

Run: ./venv/bin/python tests/test_scraper.py

Stubs _NoSaveYoutubeDL (the flat/tab pass) and enrich_with_timestamp (the
per-video pass), then drives list_recent() against a fake channel whose videos
are all newer than its streams - the shape that used to crowd every stream out
of the list. Also covers the availability / not-yet-finished filters in
fetch_flat_candidates().
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("REPO_DIR") or (os.path.dirname(HERE) if os.path.basename(HERE) == "tests" else HERE)
sys.path.insert(0, REPO)

import list_channel_videos as lcv  # noqa: E402

CHANNEL = "https://www.youtube.com/@FakeChannel/videos"
RESULTS = []


def check(name, ok, detail: object = ""):
    RESULTS.append((name, bool(ok), detail))


# --- canned tab payloads ----------------------------------------------------
def entry(vid, live_status=None, availability="public"):
    return {
        "id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "availability": availability,
        "live_status": live_status,
    }


# 20 plain uploads, newest of everything; 15 streams (12 watchable + 1 running +
# 1 upcoming + 1 member-only); 1 member-only upload.
ENTRIES = {
    "videos": [entry(f"v{i:02d}") for i in range(1, 21)] + [entry("v99", availability="subscriber_only")],
    "streams": [entry(f"s{i:02d}", "was_live") for i in range(1, 13)]
    + [entry("slive", "is_live"), entry("supcoming", "is_upcoming"), entry("smember", "was_live", "subscriber_only")],
}
# Videos newer than streams, so a single combined newest-first cap would keep
# only uploads - the bug this guards against.
STREAM_BASE = 1_600_000_000
VIDEO_BASE = 1_700_000_000


class FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False, process=True):
        tab = url.rstrip("/").rsplit("/", 1)[-1]
        return {"entries": ENTRIES.get(tab, [])}


def fake_enrich(candidate, retries=3):
    idx = int(candidate["id"][1:]) if candidate["id"][1:].isdigit() else 1
    base = VIDEO_BASE if candidate["kind"] == "video" else STREAM_BASE
    return {**candidate, "title": f"title {candidate['id']}", "timestamp": base + idx,
            "upload_date": "20260101", "duration": 60}


lcv._NoSaveYoutubeDL = FakeYDL
lcv.enrich_with_timestamp = fake_enrich
lcv._load_cache = lambda: {}
lcv._save_cache = lambda: None
lcv.REQUEST_DELAY = 0  # no real requests happen; don't sleep between them

# --- filters ----------------------------------------------------------------
flat_videos = lcv.fetch_flat_candidates(CHANNEL, "videos", "video")
flat_streams = lcv.fetch_flat_candidates(CHANNEL, "streams", "live")
check("member-only upload filtered out", "v99" not in {c["id"] for c in flat_videos})
check("member-only stream filtered out", "smember" not in {c["id"] for c in flat_streams})
check("running stream not selected yet", "slive" not in {c["id"] for c in flat_streams})
check("upcoming stream not selected yet", "supcoming" not in {c["id"] for c in flat_streams})
check("finished stream kept", "s01" in {c["id"] for c in flat_streams}, sorted(c["id"] for c in flat_streams))

# --- the list itself --------------------------------------------------------
got = lcv.list_recent(CHANNEL, 20)
kinds = [r["kind"] for r in got]
kept_ids = {r["id"] for r in got}
check("20 videos kept", kinds.count("video") == 20, kinds.count("video"))
check(
    f"{lcv.STREAM_KEEP} streams kept", kinds.count("live") == lcv.STREAM_KEEP, kinds.count("live")
)
check(
    "streams survive newer uploads",
    kept_ids >= {f"s{i:02d}" for i in range(3, 13)},
    sorted(kept_ids),
)
check("no not-yet-watchable ids in the list", not ({"slive", "supcoming", "smember", "v99"} & kept_ids))
check(
    "newest first",
    [r["timestamp"] for r in got] == sorted((r["timestamp"] for r in got), reverse=True),
)
check("stream_count is tunable", len(lcv.list_recent(CHANNEL, 20, 2)) == 22, len(lcv.list_recent(CHANNEL, 20, 2)))

# --- a channel with no /streams tab at all ----------------------------------
no_streams = "https://www.youtube.com/@FakeChannel/videos"
ENTRIES.pop("streams")
check("channel without streams still returns its videos", len(lcv.list_recent(no_streams, 20)) == 20)

print()
width = max(len(name) for name, _, _ in RESULTS)
failed = 0
for name, ok, detail in RESULTS:
    if not ok:
        failed += 1
        print(f"FAIL  {name:<{width}}  {detail}")
    else:
        print(f"ok    {name}")
print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} checks passed")
sys.exit(1 if failed else 0)
