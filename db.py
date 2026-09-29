"""SQLite persistence for scraped channel/video data.

Single-file DB shared by the scraper (writer) and the web viewer (reader).
WAL mode lets the web process read while the hourly scrape is committing.
Every call opens its own short-lived connection so the module is safe to use
from multiple threads/processes without sharing a connection object.
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parent / "watcher.db"
DB_PATH = Path(os.environ.get("DB_PATH", str(DEFAULT_DB_PATH)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT UNIQUE NOT NULL,
    name            TEXT,
    added_at        TEXT NOT NULL,
    last_scraped_at TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,
    channel_id    INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    upload_date   TEXT,
    timestamp     INTEGER NOT NULL DEFAULT 0,
    duration      INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_channel
    ON videos(channel_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_videos_timestamp
    ON videos(timestamp DESC);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,
    channels_ok     INTEGER NOT NULL DEFAULT 0,
    channels_failed INTEGER NOT NULL DEFAULT 0,
    videos_upserted INTEGER NOT NULL DEFAULT 0
);

-- Gemini analysis state per video. A video with no row here has never been
-- handed to the worker; the worker claims one, then submits the answer.
CREATE TABLE IF NOT EXISTS analyses (
    video_id    TEXT PRIMARY KEY REFERENCES videos(video_id) ON DELETE CASCADE,
    status      TEXT NOT NULL,              -- running | done | error
    question    TEXT NOT NULL,
    answer      TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    model_mode  TEXT,
    word_count  INTEGER,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_analyses_status ON analyses(status);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_words(text: str | None) -> int:
    return len((text or "").split())


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _backfill_word_counts(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT video_id, answer FROM analyses "
        "WHERE word_count IS NULL AND answer IS NOT NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE analyses SET word_count = ? WHERE video_id = ?",
            (count_words(row["answer"]), row["video_id"]),
        )


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        # Migrate DBs created before word_count existed, then backfill it.
        _ensure_column(conn, "analyses", "word_count", "INTEGER")
        _backfill_word_counts(conn)


def upsert_channel(url: str, name: str | None = None) -> int:
    """Return the channel row id, inserting it on first sight. Only overwrites
    the name when a non-empty one is supplied."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO channels (url, name, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(url) DO NOTHING",
            (url, name, now_iso()),
        )
        if name:
            conn.execute("UPDATE channels SET name = ? WHERE url = ?", (name, url))
        row = conn.execute("SELECT id FROM channels WHERE url = ?", (url,)).fetchone()
        return row["id"]


def mark_channel_scraped(channel_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE channels SET last_scraped_at = ? WHERE id = ?",
            (now_iso(), channel_id),
        )


def upsert_videos(channel_id: int, videos: list[dict]) -> int:
    """Insert/refresh a channel's videos. first_seen_at is preserved across
    reruns; last_seen_at always advances. Returns the number of rows written."""
    if not videos:
        return 0
    seen_at = now_iso()
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO videos (
                video_id, channel_id, url, title, kind, upload_date,
                timestamp, duration, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                channel_id   = excluded.channel_id,
                url          = excluded.url,
                title        = excluded.title,
                kind         = excluded.kind,
                upload_date  = excluded.upload_date,
                timestamp    = excluded.timestamp,
                duration     = excluded.duration,
                last_seen_at = excluded.last_seen_at
            """,
            [
                (
                    v["id"],
                    channel_id,
                    v["url"],
                    v.get("title", ""),
                    v.get("kind", ""),
                    v.get("upload_date"),
                    v.get("timestamp") or 0,
                    v.get("duration"),
                    seen_at,
                    seen_at,
                )
                for v in videos
            ],
        )
        return len(videos)


def record_scrape_start() -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO scrape_runs (started_at, status) VALUES (?, ?)",
            (now_iso(), "running"),
        )
        return cur.lastrowid


def record_scrape_finish(
    run_id: int,
    status: str,
    channels_ok: int,
    channels_failed: int,
    videos_upserted: int,
) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE scrape_runs SET finished_at = ?, status = ?, channels_ok = ?, "
            "channels_failed = ?, videos_upserted = ? WHERE id = ?",
            (now_iso(), status, channels_ok, channels_failed, videos_upserted, run_id),
        )


def get_channels() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.id) AS video_count,
                   (SELECT MAX(v.timestamp) FROM videos v WHERE v.channel_id = c.id)
                       AS latest_timestamp
            FROM channels c
            ORDER BY c.name COLLATE NOCASE, c.url
            """
        ).fetchall()


def get_channel(channel_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM channels WHERE id = ?", (channel_id,)
        ).fetchone()


# --- Unified video + analysis listing ---------------------------------------
#
# The viewer lists videos once, with their analysis state attached, instead of
# a videos table and a separate analyses table you have to cross-reference.
# The buckets below partition the videos table - every video is in exactly one
# of none/running/done/short/error/invalid - which is what the filter bar shows.
COMBINED_STATUSES = ("none", "running", "done", "short", "error", "invalid")

_COMBINED_FROM = """
    FROM videos v
    JOIN channels c ON c.id = v.channel_id
    LEFT JOIN analyses a ON a.video_id = v.video_id
"""

_COMBINED_SELECT = """
    SELECT v.*, c.name AS channel_name, c.url AS channel_url,
           a.status      AS analysis_status,
           a.word_count  AS word_count,
           a.attempts    AS attempts,
           a.error       AS analysis_error,
           a.model_mode  AS model_mode,
           a.started_at  AS analysis_started_at,
           a.finished_at AS finished_at,
           a.status IS NOT NULL AS has_analysis,
           a.answer IS NOT NULL AS has_answer
"""


def _status_clause(status: str | None, min_words: int) -> tuple[str, list]:
    """SQL predicate (no WHERE) plus params for one analysis bucket.

    'short' uses the same rule as count_analyses(): a done answer below
    min_words. A done row with a NULL word_count counts as done, not short, so
    the buckets stay consistent with the queue's own accounting."""
    if status == "none":
        return "a.video_id IS NULL", []
    if status == "running":
        return "a.status = 'running'", []
    if status == "done":
        return "a.status = 'done' AND (a.word_count IS NULL OR a.word_count >= ?)", [min_words]
    if status == "short":
        return "a.status = 'done' AND a.word_count IS NOT NULL AND a.word_count < ?", [min_words]
    if status == "error":
        return "a.status = 'error'", []
    if status == "invalid":
        return "a.status = 'invalid'", []
    return "", []


def _like_pattern(term: str) -> str:
    """Escape a user-supplied search term so % and _ match literally."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _combined_where(
    status: str | None, channel_id: int | None, q: str | None, min_words: int
) -> tuple[str, list]:
    conditions: list[str] = []
    params: list = []
    clause, clause_params = _status_clause(status, min_words)
    if clause:
        conditions.append(clause)
        params.extend(clause_params)
    if channel_id is not None:
        conditions.append("v.channel_id = ?")
        params.append(channel_id)
    if q:
        conditions.append("v.title LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(q))
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


def get_combined_videos(
    status: str | None = None,
    channel_id: int | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    min_words: int = 50,
) -> list[sqlite3.Row]:
    """Newest first, one row per video, analysis columns folded in."""
    where, params = _combined_where(status, channel_id, q, min_words)
    with get_connection() as conn:
        return conn.execute(
            _COMBINED_SELECT + _COMBINED_FROM + where
            + " ORDER BY v.timestamp DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()


def count_combined_videos(
    status: str | None = None,
    channel_id: int | None = None,
    q: str | None = None,
    min_words: int = 50,
) -> int:
    where, params = _combined_where(status, channel_id, q, min_words)
    with get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n" + _COMBINED_FROM + where, params
        ).fetchone()["n"]


def combined_counts(
    min_words: int = 50, max_attempts: int = 3, stale_minutes: int = 120
) -> dict:
    """Per-bucket video counts for the filter bar, plus 'pending' - how many
    videos the worker would consider due (see _analysis_eligible_clause)."""
    with get_connection() as conn:
        counts = {"all": conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]}
        for bucket in COMBINED_STATUSES:
            clause, params = _status_clause(bucket, min_words)
            counts[bucket] = conn.execute(
                "SELECT COUNT(*) AS n" + _COMBINED_FROM + f" WHERE {clause}", params
            ).fetchone()["n"]
        counts["pending"] = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM videos v
            LEFT JOIN analyses a ON a.video_id = v.video_id
            WHERE {_analysis_eligible_clause()}
            """,
            (max_attempts, min_words, max_attempts, now_iso()),
        ).fetchone()["n"]
        return counts


def get_recent_scrape_runs(limit: int = 10) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def _analysis_eligible_clause() -> str:
    """Placeholders, in order: max_attempts, min_words, max_attempts, stale_before."""
    return (
        "a.video_id IS NULL "
        "OR a.status = 'invalid' "
        "OR (a.status = 'error' AND a.attempts < ?) "
        "OR (a.status = 'done' AND a.word_count IS NOT NULL "
        "    AND a.word_count < ? AND a.attempts < ?) "
        "OR (a.status = 'running' AND a.started_at < ?)"
    )


def claim_next_analysis(
    max_attempts: int = 3, stale_minutes: int = 120, min_words: int = 50
) -> dict | None:
    """Atomically claim the next video to analyze and mark it 'running'.

    Priority: videos the user explicitly marked 'invalid' first, then retries
    (errors / soft-failed short answers / stuck runs), then never-analyzed
    videos. Retries go first so failures clear within hours instead of waiting
    behind the whole backlog; the max_attempts cap stops a permanently failing
    video from blocking new uploads. Newest first within each group. Returns
    None when nothing is due; the returned dict's 'retry' flag is True when this
    is a re-analysis.
    """
    stale_before = (
        datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    ).isoformat()

    conn = get_connection()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"""
            SELECT v.video_id, v.url, v.title, v.channel_id, a.attempts
            FROM videos v
            LEFT JOIN analyses a ON a.video_id = v.video_id
            WHERE {_analysis_eligible_clause()}
            ORDER BY CASE
                WHEN a.status = 'invalid' THEN 0
                WHEN a.video_id IS NULL THEN 2
                ELSE 1
            END, v.timestamp DESC
            LIMIT 1
            """,
            (max_attempts, min_words, max_attempts, stale_before),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None

        now = now_iso()
        conn.execute(
            """
            INSERT INTO analyses (video_id, status, question, attempts, created_at, started_at)
            VALUES (?, 'running', ?, 1, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                status     = 'running',
                started_at = excluded.started_at,
                attempts   = analyses.attempts + 1,
                error      = NULL
            """,
            (row["video_id"], row["url"], now, now),
        )
        conn.execute("COMMIT")
        return {
            "video_id": row["video_id"],
            "url": row["url"],
            "title": row["title"],
            "channel_id": row["channel_id"],
            "question": row["url"],
            "attempts": (row["attempts"] or 0) + 1,
            # True when the video already had an analysis (re-analysis, so the
            # worker must not reuse a cached result file).
            "retry": row["attempts"] is not None,
        }
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def submit_analysis(
    video_id: str,
    answer: str | None = None,
    error: str | None = None,
    model_mode: str | None = None,
) -> bool:
    """Record a worker's result. Returns False if the video was never claimed."""
    status = "error" if error else "done"
    word_count = count_words(answer) if answer else None
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE analyses
            SET status = ?, answer = ?, error = ?, word_count = ?,
                model_mode = COALESCE(?, model_mode), finished_at = ?
            WHERE video_id = ?
            """,
            (status, answer, error, word_count, model_mode, now_iso(), video_id),
        )
        return cur.rowcount > 0


def mark_analysis_invalid(video_id: str) -> bool:
    """Flag an analysis for re-analysis. It keeps its old answer for reference
    but jumps the queue (see claim_next_analysis) and attempts reset so the
    retry has the full budget. Returns False if the video has no analysis."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE analyses
            SET status = 'invalid', attempts = 0, error = NULL, finished_at = NULL
            WHERE video_id = ?
            """,
            (video_id,),
        )
        return cur.rowcount > 0


def abandon_running_analyses(reason: str = "Abandoned by a previous worker run") -> int:
    """Recover rows left 'running' by a worker that was killed mid-run. Called
    by a fresh worker at startup (before it claims anything), when no other
    worker can be active. Returns the number of rows recovered."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE analyses SET status = 'error', error = ?, finished_at = ? "
            "WHERE status = 'running'",
            (reason, now_iso()),
        )
        return cur.rowcount


def get_analyses(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    max_words: int | None = None,
) -> list[sqlite3.Row]:
    base = """
        SELECT a.*, v.title, v.url, v.upload_date, v.timestamp, v.duration,
               c.name AS channel_name
        FROM analyses a
        JOIN videos v ON v.video_id = a.video_id
        JOIN channels c ON c.id = v.channel_id
    """
    conditions: list[str] = []
    params: list = []
    if status:
        conditions.append("a.status = ?")
        params.append(status)
    if max_words is not None:
        conditions.append("a.word_count IS NOT NULL AND a.word_count < ?")
        params.append(max_words)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as conn:
        return conn.execute(
            base + where
            + " ORDER BY COALESCE(a.finished_at, a.started_at, a.created_at) DESC"
            " LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()


def get_analysis(video_id: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT a.*, v.title, v.url, v.upload_date, v.timestamp, v.duration,
                   c.name AS channel_name
            FROM analyses a
            JOIN videos v ON v.video_id = a.video_id
            JOIN channels c ON c.id = v.channel_id
            WHERE a.video_id = ?
            """,
            (video_id,),
        ).fetchone()


def count_analyses(max_attempts: int = 3, min_words: int = 50) -> dict:
    """Status counts, the number of eligible (pending) videos, and word stats
    for completed answers. 'short' counts done answers below min_words - these
    are soft failures worth re-running."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM analyses GROUP BY status"
        ).fetchall()
        counts = {"running": 0, "done": 0, "error": 0, "invalid": 0}
        counts.update({r["status"]: r["n"] for r in rows})
        counts["pending"] = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM videos v
            LEFT JOIN analyses a ON a.video_id = v.video_id
            WHERE {_analysis_eligible_clause()}
            """,
            (max_attempts, min_words, max_attempts, now_iso()),
        ).fetchone()["n"]
        counts["short"] = conn.execute(
            "SELECT COUNT(*) AS n FROM analyses "
            "WHERE status = 'done' AND word_count IS NOT NULL AND word_count < ?",
            (min_words,),
        ).fetchone()["n"]
        avg = conn.execute(
            "SELECT AVG(word_count) AS a FROM analyses "
            "WHERE status = 'done' AND word_count IS NOT NULL"
        ).fetchone()["a"]
        counts["avg_words"] = int(avg) if avg else 0
        counts["min_words"] = min_words
        return counts


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
