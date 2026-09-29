"""Exercise the unified list UI against a snapshot of the production DB.

Run: DB_PATH=<snapshot> ANALYSIS_TOKEN=test-token ./venv/bin/python test_ui.py
Mutates only the snapshot (invalidate + a synthetic short answer).
"""
import os
import re
import sys
from html import escape as html_escape

REPO = "/home/lathif/github/axeven/youtube-watcher"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("REPO_DIR") or (os.path.dirname(HERE) if os.path.basename(HERE) == "tests" else HERE)
sys.path.insert(0, REPO)
os.environ.setdefault("ANALYSIS_TOKEN", "test-token")

import db  # noqa: E402
from web_app import app  # noqa: E402

client = app.test_client()
buckets = db.combined_counts(min_words=50)
# Content-dependent expectations are derived from the snapshot, so this stays
# valid as the queue fills up.
total_videos = db.count_combined_videos(min_words=50)
channel2_expected = db.count_combined_videos(channel_id=2, min_words=50)
with db.get_connection() as conn:
    rupiah_expected = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE title LIKE '%rupiah%'"
    ).fetchone()["n"]
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def rows(html):
    """Count video rows: each one carries exactly one kind/duration sub-line.
    (Counting <tr><td> would also catch the 10-row scrape-runs table.)"""
    return html.count('<div class="sub">')


# --- the list itself --------------------------------------------------------
r = client.get("/")
html = r.get_data(as_text=True)
check("GET / is 200", r.status_code == 200, r.status_code)
check("unified heading present", "Videos &amp; analyses" in html)
check("default page shows 50 rows", rows(html) == 50, rows(html))
# The Video column is the analysis link; YouTube is the icon link beside it.
page_rows = db.get_combined_videos(limit=50, min_words=50)
analyzed_in_page = sum(1 for v in page_rows if v["has_analysis"])
check(
    f"analyzed titles link to /analysis/ ({analyzed_in_page} of 50 rows)",
    html.count('href="/analysis/') == analyzed_in_page,
    html.count('href="/analysis/'),
)
check(
    "unanalyzed titles are plain text",
    all(
        f'href="/analysis/{v["video_id"]}"' not in html
        for v in page_rows
        if not v["has_analysis"]
    ),
)
check("every row has the youtube icon link", html.count('class="ext"') == 50, html.count('class="ext"'))
check(
    "icon links open the video in a new tab",
    html.count('target="_blank" rel="noopener"') == 50,
    html.count('target="_blank" rel="noopener"'),
)
_link_row = next(v for v in page_rows if v["has_analysis"])
_link_title = html_escape(_link_row["title"]).replace("&#x27;", "&#39;")
check(
    "analysis link wraps the title text",
    f'<a href="/analysis/{_link_row["video_id"]}">{_link_title}</a>' in html,
    _link_row["video_id"],
)
check("old 'answer' column link is gone", ">answer</a>" not in html)
check("not-analyzed badge shown", "not analyzed" in html)
check("subtitle counts", f"of {buckets['all']} videos shown" in html)
check("filter bar counts rendered", f"Done ({buckets['done']})" in html and f"Not analyzed ({buckets['none']})" in html)
check("toolbar present", 'class="toolbar"' in html and 'name="q"' in html)

# --- every bucket filter returns exactly its bucket ------------------------
for bucket in db.COMBINED_STATUSES:
    body = client.get(f"/?status={bucket}&limit=500").get_data(as_text=True)
    check(
        f"/?status={bucket} → {buckets[bucket]} rows",
        rows(body) == buckets[bucket],
        f"expected {buckets[bucket]}, got {rows(body)}",
    )

body = client.get("/?status=bogus&limit=500").get_data(as_text=True)
check("unknown status ignored (all rows)", rows(body) == min(buckets["all"], 500), rows(body))

# --- channel + search + sticky filters -------------------------------------
body = client.get("/?q=rupiah&limit=500").get_data(as_text=True)
check(f"search 'rupiah' → {rupiah_expected} rows", rows(body) == rupiah_expected, rows(body))
body = client.get("/?channel=2&limit=500").get_data(as_text=True)
check(f"channel filter → {channel2_expected} rows", rows(body) == channel2_expected, rows(body))
body = client.get("/?channel=2&q=zzzznothing").get_data(as_text=True)
check("combined filters → 0 rows + empty state", rows(body) == 0 and "Nothing matches" in body)
body = client.get("/?status=done&q=rupiah").get_data(as_text=True)
check("bucket links keep other filters", "q=rupiah" in body and "status=short" in body)

# --- paging ----------------------------------------------------------------
body = client.get("/?limit=10").get_data(as_text=True)
check("pager range text", f"Showing 1-10 of {total_videos}" in body, body[body.find("Showing"):body.find("Showing")+30] if "Showing" in body else "")
body = client.get("/?limit=10&offset=10").get_data(as_text=True)
check("older+newer links", "Newer" in body and "Older" in body)

# --- legacy URLs -----------------------------------------------------------
r = client.get("/analyses")
check("/analyses → /?status=done", r.status_code == 302 and r.headers["Location"].endswith("/?status=done"), r.headers.get("Location"))
r = client.get("/analyses?status=short&limit=10")
check("/analyses keeps status+limit", r.headers["Location"].endswith("/?status=short&limit=10"), r.headers.get("Location"))
r = client.get("/channel/2")
check("/channel/2 → /?channel=2", r.status_code == 302 and r.headers["Location"].endswith("/?channel=2"), r.headers.get("Location"))
check("/channel/999 is 404", client.get("/channel/999").status_code == 404)

# --- api -------------------------------------------------------------------
j = client.get("/api/videos?limit=2").get_json()
check(f"/api/videos: total {total_videos}", j["total"] == total_videos, j["total"])
check("/api/videos: analysis fields", all(k in j["videos"][0] for k in ("analysis_status", "word_count", "has_answer", "attempts")))
check("/api/videos: buckets block", j["buckets"]["done"] == buckets["done"])
check("/api/videos?status=none total", client.get("/api/videos?status=none&limit=1").get_json()["total"] == buckets["none"])
check("/api/videos?channel_id back-compat", client.get("/api/videos?channel_id=2&limit=1").get_json()["total"] == channel2_expected)
ja = client.get("/api/analyses?limit=1").get_json()
check("/api/analyses shape unchanged", set(ja) == {"counts", "count", "limit", "offset", "analyses"} and "answer" in ja["analyses"][0])

# --- worker token paths ----------------------------------------------------
check("claim without token → 401", client.post("/api/analyses/claim").status_code == 401)
check("claim with wrong token → 401", client.post("/api/analyses/claim", headers={"X-Analysis-Token": "nope"}).status_code == 401)
check("claim with right token → 200/204", client.post("/api/analyses/claim", headers={"X-Analysis-Token": "test-token"}).status_code in (200, 204))

# --- detail page + invalidate ---------------------------------------------
vid = db.get_combined_videos(status="done", limit=1, min_words=50)[0]["video_id"]
check("detail page 200", client.get(f"/analysis/{vid}").status_code == 200)
check("detail page 404s on junk", client.get("/analysis/nope-not-a-video").status_code == 404)

r = client.post(f"/analysis/{vid}/invalid", data={"next": "/?status=none"})
check("invalidate honours ?next", r.status_code == 302 and r.headers["Location"] == "/?status=none", r.headers.get("Location"))
after = db.combined_counts(min_words=50)
check("invalid bucket grew, done shrank", after["invalid"] == buckets["invalid"] + 1 and after["done"] == buckets["done"] - 1, (after["invalid"], after["done"]))
r = client.post(f"/analysis/{vid}/invalid", data={"next": "//evil.example.com"})
check("open redirect refused", "evil" not in (r.headers.get("Location") or ""), r.headers.get("Location"))
r = client.post(f"/analysis/{vid}/invalid", data={})
check("no ?next → returns to detail page", (r.headers.get("Location") or "").endswith(f"/analysis/{vid}"), r.headers.get("Location"))

# --- a synthetic short answer, since prod has none right now --------------
before_short = db.combined_counts(min_words=50)
target = db.get_combined_videos(status="none", limit=1, min_words=50)[0]["video_id"]
with db.get_connection() as conn:
    conn.execute(
        "INSERT INTO analyses (video_id, status, question, answer, attempts, word_count, "
        "created_at, started_at, finished_at) VALUES (?, 'done', 'x', 'too short', 1, 5, ?, ?, ?)",
        (target, db.now_iso(), db.now_iso(), db.now_iso()),
    )
short_buckets = db.combined_counts(min_words=50)
check("short bucket picks up the 5-word answer", short_buckets["short"] == before_short["short"] + 1, short_buckets["short"])
check("short is excluded from done", short_buckets["done"] == before_short["done"], short_buckets["done"])
check("buckets still partition the table", sum(short_buckets[b] for b in db.COMBINED_STATUSES) == short_buckets["all"])
body = client.get("/?status=short&limit=500").get_data(as_text=True)
check("short filter renders the row with a badge", rows(body) == 1 and "badge short" in body and "5 words" in body)
check("short row shows done+short badges", "badge done" in body)

print()
width = max(len(name) for name, _, _ in results)
failed = 0
for name, ok, detail in results:
    if not ok:
        failed += 1
        print(f"FAIL  {name:<{width}}  {detail}")
    else:
        print(f"ok    {name}")
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
