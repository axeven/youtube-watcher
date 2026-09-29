#!/usr/bin/env python3
"""Web viewer + analysis queue API for scraped YouTube channel data.

The viewer is one list: every video with its analysis state attached (see
db.get_combined_videos), filtered by analysis bucket, channel or title search.
The /api/analyses/* endpoints are used by the local analyze_worker.py to claim
a video and submit the Gemini answer. Run directly (`python web_app.py`) for
local dev, or under gunicorn in the container
(`gunicorn --bind 0.0.0.0:8000 web_app:app`).
"""
import hmac
import os
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

import db

UTC_PLUS_7 = timezone(timedelta(hours=7))
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
# Done answers below this many words are treated as soft failures.
MIN_ANSWER_WORDS = int(os.environ.get("ANALYSIS_MIN_WORDS", "50"))


def format_duration(seconds) -> str:
    if not seconds:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_timestamp(ts) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts), UTC_PLUS_7).strftime("%Y-%m-%d %H:%M")


def video_to_dict(row) -> dict:
    """A unified list row: the video's own fields plus its analysis state,
    which is all None when the video has never been claimed.

    `has_analysis` means the video has an analyses row at all (so its detail
    page renders, even for error/invalid/running); `has_answer` means that row
    carries the Gemini answer.
    """
    word_count = row["word_count"]
    return {
        "video_id": row["video_id"],
        "channel_id": row["channel_id"],
        "channel_name": row["channel_name"],
        "channel_url": row["channel_url"],
        "title": row["title"],
        "kind": row["kind"],
        "url": row["url"],
        "upload_date": row["upload_date"],
        "timestamp": row["timestamp"],
        "duration": row["duration"],
        "duration_display": format_duration(row["duration"]),
        "uploaded_display": format_timestamp(row["timestamp"]),
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "analysis_status": row["analysis_status"],
        "word_count": word_count,
        "attempts": row["attempts"],
        "analysis_started_at": row["analysis_started_at"],
        "finished_at": row["finished_at"],
        "analysis_error": row["analysis_error"],
        "has_analysis": bool(row["has_analysis"]),
        "has_answer": bool(row["has_answer"]),
        # Same rule as db.count_analyses(): a finished answer that is too short.
        "short": row["analysis_status"] == "done"
        and word_count is not None
        and word_count < MIN_ANSWER_WORDS,
    }


def parse_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def analysis_to_dict(row) -> dict:
    keys = row.keys()
    answer = row["answer"]
    word_count = row["word_count"] if "word_count" in keys else None
    if word_count is None:
        word_count = db.count_words(answer)
    return {
        "video_id": row["video_id"],
        "status": row["status"],
        "question": row["question"],
        "answer": answer,
        "error": row["error"],
        "attempts": row["attempts"],
        "model_mode": row["model_mode"],
        "word_count": word_count,
        "short": row["status"] == "done" and answer is not None
        and word_count < MIN_ANSWER_WORDS,
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "title": row["title"] if "title" in keys else None,
        "url": row["url"] if "url" in keys else None,
        "channel_name": row["channel_name"] if "channel_name" in keys else None,
        "upload_date": row["upload_date"] if "upload_date" in keys else None,
        "timestamp": row["timestamp"] if "timestamp" in keys else None,
    }


def list_filters() -> dict:
    """The unified list's filters, read off the query string.

    `status` is one of db.COMBINED_STATUSES - an unknown value means "no
    filter" - plus an optional channel id and a title search term."""
    status = request.args.get("status") or None
    if status not in db.COMBINED_STATUSES:
        status = None
    return {
        "status": status,
        "channel_id": request.args.get("channel", type=int),
        "q": (request.args.get("q") or "").strip() or None,
    }


def create_app() -> Flask:
    app = Flask(__name__)
    app.jinja_env.filters["duration"] = format_duration
    app.jinja_env.filters["ts"] = format_timestamp

    def token_ok() -> bool:
        """Validate the worker's shared token. No token configured => open
        (dev). Accepts X-Analysis-Token or Authorization: Bearer."""
        token = os.environ.get("ANALYSIS_TOKEN", "")
        if not token:
            return True
        supplied = request.headers.get("X-Analysis-Token", "")
        if not supplied:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[len("Bearer "):]
        return hmac.compare_digest(supplied, token)

    @app.route("/")
    def index():
        """The single list: every video, newest first, with its analysis state
        and filters over the analysis buckets."""
        filters = list_filters()
        limit = min(max(parse_int(request.args.get("limit"), DEFAULT_LIMIT), 1), MAX_LIMIT)
        offset = max(parse_int(request.args.get("offset"), 0), 0)

        def list_url(**overrides) -> str:
            """This view with some args swapped in, so the bucket links, the
            channel picker and paging all keep the other filters."""
            args = {
                "status": filters["status"],
                "channel": filters["channel_id"],
                "q": filters["q"],
                "limit": limit,
                "offset": offset,
            }
            args.update(overrides)
            return url_for("index", **{k: v for k, v in args.items() if v not in (None, "", 0)})

        videos = [
            video_to_dict(v)
            for v in db.get_combined_videos(
                status=filters["status"],
                channel_id=filters["channel_id"],
                q=filters["q"],
                limit=limit,
                offset=offset,
                min_words=MIN_ANSWER_WORDS,
            )
        ]
        return render_template(
            "index.html",
            channels=db.get_channels(),
            videos=videos,
            total=db.count_combined_videos(
                status=filters["status"],
                channel_id=filters["channel_id"],
                q=filters["q"],
                min_words=MIN_ANSWER_WORDS,
            ),
            grand_total=db.count_combined_videos(min_words=MIN_ANSWER_WORDS),
            buckets=db.combined_counts(min_words=MIN_ANSWER_WORDS),
            analyses=db.count_analyses(min_words=MIN_ANSWER_WORDS),
            runs=db.get_recent_scrape_runs(),
            limit=limit,
            offset=offset,
            list_url=list_url,
            **filters,
        )

    @app.route("/analyses")
    def analyses():
        """Kept for old bookmarks: analyses are a filter on the unified list
        now, defaulting to the finished answers."""
        status = request.args.get("status") or "done"
        if status not in db.COMBINED_STATUSES:
            status = None
        args = {"status": status}
        for key in ("channel", "q", "limit", "offset"):
            value = request.args.get(key)
            if value:
                args[key] = value
        return redirect(url_for("index", **args))

    @app.route("/channel/<int:channel_id>")
    def channel(channel_id: int):
        """Kept for old bookmarks: a channel is a filter on the unified list."""
        if db.get_channel(channel_id) is None:
            abort(404)
        return redirect(url_for("index", channel=channel_id))

    @app.route("/api/videos")
    def api_videos():
        """Every video with its analysis state, same filters as the list."""
        filters = list_filters()
        # `channel_id` is the older spelling of this filter; keep it working.
        channel_id = filters["channel_id"]
        if channel_id is None:
            channel_id = request.args.get("channel_id", type=int)
        limit = min(max(parse_int(request.args.get("limit"), DEFAULT_LIMIT), 1), MAX_LIMIT)
        offset = max(parse_int(request.args.get("offset"), 0), 0)
        videos = db.get_combined_videos(
            status=filters["status"],
            channel_id=channel_id,
            q=filters["q"],
            limit=limit,
            offset=offset,
            min_words=MIN_ANSWER_WORDS,
        )
        return jsonify(
            {
                "count": len(videos),
                "total": db.count_combined_videos(
                    status=filters["status"],
                    channel_id=channel_id,
                    q=filters["q"],
                    min_words=MIN_ANSWER_WORDS,
                ),
                "limit": limit,
                "offset": offset,
                "buckets": db.combined_counts(min_words=MIN_ANSWER_WORDS),
                "videos": [video_to_dict(v) for v in videos],
            }
        )

    @app.route("/api/channels")
    def api_channels():
        return jsonify(
            {
                "channels": [
                    {
                        "id": c["id"],
                        "url": c["url"],
                        "name": c["name"],
                        "video_count": c["video_count"],
                        "last_scraped_at": c["last_scraped_at"],
                    }
                    for c in db.get_channels()
                ]
            }
        )

    @app.route("/analysis/<video_id>")
    def analysis(video_id: str):
        row = db.get_analysis(video_id)
        if row is None:
            abort(404)
        return render_template("analysis.html", a=analysis_to_dict(row))

    @app.route("/analysis/<video_id>/invalid", methods=["POST"])
    def invalidate_analysis(video_id: str):
        db.mark_analysis_invalid(video_id)
        # The list posts here with ?next=<this view>, so re-queuing from the
        # list comes back to the same filters instead of the detail page.
        target = request.form.get("next") or request.args.get("next")
        if target and target.startswith("/") and not target.startswith("//"):
            return redirect(target)
        return redirect(url_for("analysis", video_id=video_id))

    @app.route("/api/analyses/claim", methods=["POST"])
    def api_claim_analysis():
        if not token_ok():
            return jsonify({"error": "unauthorized"}), 401
        claimed = db.claim_next_analysis(min_words=MIN_ANSWER_WORDS)
        if claimed is None:
            return ("", 204)
        return jsonify(claimed)

    @app.route("/api/analyses/<video_id>", methods=["POST"])
    def api_submit_analysis(video_id: str):
        if not token_ok():
            return jsonify({"error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        answer = data.get("answer")
        error = data.get("error")
        if not answer and not error:
            return jsonify({"error": "provide 'answer' or 'error'"}), 400
        stored = db.submit_analysis(
            video_id, answer=answer, error=error, model_mode=data.get("model_mode")
        )
        if not stored:
            return jsonify({"error": "unknown or unclaimed video"}), 404
        return jsonify({"status": "ok", "video_id": video_id})

    @app.route("/api/analyses/abandon-running", methods=["POST"])
    def api_abandon_running():
        if not token_ok():
            return jsonify({"error": "unauthorized"}), 401
        return jsonify({"abandoned": db.abandon_running_analyses()})

    @app.route("/api/analyses")
    def api_analyses():
        limit = min(max(parse_int(request.args.get("limit"), DEFAULT_LIMIT), 1), MAX_LIMIT)
        offset = max(parse_int(request.args.get("offset"), 0), 0)
        status = request.args.get("status") or None
        max_words = MIN_ANSWER_WORDS if status == "short" else None
        rows = db.get_analyses(
            limit=limit, offset=offset,
            status=status if status != "short" else None,
            max_words=max_words,
        )
        return jsonify(
            {
                "counts": db.count_analyses(min_words=MIN_ANSWER_WORDS),
                "count": len(rows),
                "limit": limit,
                "offset": offset,
                "analyses": [analysis_to_dict(r) for r in rows],
            }
        )

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
