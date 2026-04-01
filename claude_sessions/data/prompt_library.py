"""Prompt library — save, search, and reuse effective prompts."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from ..config import settings

DB_PATH = settings.claude_data_dir / "prompt-library.db"


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prompts (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source_session TEXT,
            source_uuid TEXT,
            title TEXT NOT NULL,
            tags TEXT DEFAULT '[]',
            category TEXT DEFAULT '',
            times_used INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_used TEXT
        )
    """)
    conn.commit()
    return conn


def _make_id(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:8]


def save_prompt(
    content: str,
    source_session: str = "",
    source_uuid: str = "",
    title: str = "",
    tags: Optional[List[str]] = None,
    category: str = "",
) -> dict:
    """Save a prompt to the library."""
    prompt_id = _make_id(content)
    if not title:
        title = content[:80].strip().replace("\n", " ")
        if len(content) > 80:
            title += "..."
    now = datetime.now(timezone.utc).isoformat()

    conn = _get_db()
    conn.execute(
        """INSERT OR IGNORE INTO prompts
           (id, content, source_session, source_uuid, title, tags, category, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (prompt_id, content, source_session, source_uuid, title,
         json.dumps(tags or []), category, now),
    )
    conn.commit()
    conn.close()
    return {"id": prompt_id, "title": title}


def get_all_prompts() -> List[dict]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM prompts ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_prompt(prompt_id: str) -> Optional[dict]:
    conn = _get_db()
    row = conn.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def increment_usage(prompt_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_db()
    conn.execute(
        "UPDATE prompts SET times_used = times_used + 1, last_used = ? WHERE id = ?",
        (now, prompt_id),
    )
    conn.commit()
    conn.close()


def delete_prompt(prompt_id: str) -> bool:
    conn = _get_db()
    cursor = conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def search_prompts(query: str) -> List[dict]:
    conn = _get_db()
    rows = conn.execute(
        """SELECT * FROM prompts
           WHERE content LIKE ? OR title LIKE ? OR tags LIKE ?
           ORDER BY times_used DESC, created_at DESC""",
        (f"%{query}%", f"%{query}%", f"%{query}%"),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_categories() -> List[str]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT DISTINCT category FROM prompts WHERE category != '' ORDER BY category"
    ).fetchall()
    conn.close()
    return [r["category"] for r in rows]


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "content": row["content"],
        "source_session": row["source_session"],
        "source_uuid": row["source_uuid"],
        "title": row["title"],
        "tags": json.loads(row["tags"]),
        "category": row["category"],
        "times_used": row["times_used"],
        "created_at": row["created_at"],
        "last_used": row["last_used"],
    }
