"""Session outcome tracking — rate sessions as productive, wasted, etc."""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..config import settings

DB_PATH = settings.claude_data_dir / "session-outcomes.db"


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outcomes (
            session_id TEXT PRIMARY KEY,
            rating INTEGER NOT NULL,
            tags TEXT DEFAULT '[]',
            note TEXT DEFAULT '',
            rated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def rate_session(
    session_id: str,
    rating: int,
    tags: Optional[List[str]] = None,
    note: str = "",
) -> dict:
    """Rate a session (1-5). Upserts."""
    rating = max(1, min(5, rating))
    now = datetime.now(timezone.utc).isoformat()
    tags_json = json.dumps(tags or [])

    conn = _get_db()
    conn.execute(
        """INSERT INTO outcomes (session_id, rating, tags, note, rated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             rating=excluded.rating, tags=excluded.tags,
             note=excluded.note, rated_at=excluded.rated_at""",
        (session_id, rating, tags_json, note, now),
    )
    conn.commit()
    conn.close()
    return {"session_id": session_id, "rating": rating, "tags": tags or [], "note": note}


def get_outcome(session_id: str) -> Optional[dict]:
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM outcomes WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "session_id": row["session_id"],
        "rating": row["rating"],
        "tags": json.loads(row["tags"]),
        "note": row["note"],
        "rated_at": row["rated_at"],
    }


def get_all_outcomes() -> List[dict]:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM outcomes ORDER BY rated_at DESC").fetchall()
    conn.close()
    return [
        {
            "session_id": r["session_id"],
            "rating": r["rating"],
            "tags": json.loads(r["tags"]),
            "note": r["note"],
            "rated_at": r["rated_at"],
        }
        for r in rows
    ]


def get_outcome_stats(days: int = 30) -> dict:
    """Aggregate outcome statistics."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM outcomes WHERE rated_at > ? ORDER BY rated_at DESC",
        (cutoff,),
    ).fetchall()
    conn.close()

    if not rows:
        return {
            "total_rated": 0, "avg_rating": 0, "rating_distribution": {},
            "tag_counts": {}, "productive": 0, "wasted": 0,
        }

    ratings = [r["rating"] for r in rows]
    all_tags: List[str] = []
    for r in rows:
        all_tags.extend(json.loads(r["tags"]))

    from collections import Counter

    tag_counts = dict(Counter(all_tags))
    rating_dist = dict(Counter(ratings))

    return {
        "total_rated": len(rows),
        "avg_rating": round(sum(ratings) / len(ratings), 1),
        "rating_distribution": rating_dist,
        "tag_counts": tag_counts,
        "productive": tag_counts.get("productive", 0),
        "wasted": tag_counts.get("wasted", 0),
    }
