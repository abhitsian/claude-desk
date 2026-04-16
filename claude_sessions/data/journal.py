"""Inner Life — journal entries, memory orbs, and islands storage."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from ..config import settings

DB_PATH = settings.claude_data_dir / "journal.db"

VALID_EMOTIONS = {
    "clarity", "frustration", "momentum", "doubt", "playfulness",
    "avoidance", "breakthrough", "tension", "pride", "vulnerability",
    "focus", "scattered",
}

VALID_ISLAND_STATUSES = {"active", "fading", "merged"}


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS journal_entries (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            emotional_tone TEXT DEFAULT '',
            secondary_tones TEXT DEFAULT '[]',
            session_ids TEXT DEFAULT '[]',
            model_used TEXT,
            prompt_version TEXT DEFAULT '1',
            token_cost TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_orbs (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            content TEXT NOT NULL,
            emotion TEXT DEFAULT 'clarity',
            intensity REAL DEFAULT 0.5,
            source_session_id TEXT,
            source_context TEXT,
            is_core INTEGER DEFAULT 0,
            promoted_at TEXT,
            island_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_orbs_date ON memory_orbs(date);
        CREATE INDEX IF NOT EXISTS idx_orbs_core ON memory_orbs(is_core);
        CREATE INDEX IF NOT EXISTS idx_orbs_island ON memory_orbs(island_id);

        CREATE TABLE IF NOT EXISTS islands (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            formed_at TEXT NOT NULL,
            last_updated TEXT NOT NULL,
            strength REAL DEFAULT 0.5,
            core_memory_ids TEXT DEFAULT '[]',
            status TEXT DEFAULT 'active'
        );

        CREATE INDEX IF NOT EXISTS idx_islands_status ON islands(status);

        CREATE TABLE IF NOT EXISTS consolidations (
            id TEXT PRIMARY KEY,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            synthesis TEXT NOT NULL,
            patterns TEXT DEFAULT '[]',
            resolved_contradictions TEXT DEFAULT '[]',
            retired_patterns TEXT DEFAULT '[]',
            entry_count INTEGER DEFAULT 0,
            orb_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_consolidations_period
        ON consolidations(period_end);
    """)

    # Migration: add narrative column to islands if missing
    try:
        conn.execute("SELECT narrative FROM islands LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE islands ADD COLUMN narrative TEXT DEFAULT ''")
        conn.commit()

    # Migration: add entry_type column to journal_entries if missing
    try:
        conn.execute("SELECT entry_type FROM journal_entries LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE journal_entries ADD COLUMN entry_type TEXT DEFAULT 'daily'")
        conn.commit()

    conn.commit()
    return conn


# --- Journal Entries ---


def save_journal_entry(
    date: str,
    content: str,
    emotional_tone: str,
    secondary_tones: list = None,
    session_ids: list = None,
    model_used: str = None,
    prompt_version: str = "1",
    token_cost: dict = None,
) -> dict:
    """Save or update a journal entry for a given date."""
    conn = _get_db()
    entry_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()

    # Upsert — one entry per date
    existing = conn.execute(
        "SELECT id FROM journal_entries WHERE date = ?", (date,)
    ).fetchone()

    if existing:
        entry_id = existing["id"]
        conn.execute(
            """UPDATE journal_entries
               SET content = ?, emotional_tone = ?, secondary_tones = ?,
                   session_ids = ?, model_used = ?, prompt_version = ?, token_cost = ?
               WHERE id = ?""",
            (
                content, emotional_tone,
                json.dumps(secondary_tones or []),
                json.dumps(session_ids or []),
                model_used, prompt_version,
                json.dumps(token_cost or {}),
                entry_id,
            ),
        )
    else:
        conn.execute(
            """INSERT INTO journal_entries
               (id, date, content, emotional_tone, secondary_tones,
                session_ids, model_used, prompt_version, token_cost, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_id, date, content, emotional_tone,
                json.dumps(secondary_tones or []),
                json.dumps(session_ids or []),
                model_used, prompt_version,
                json.dumps(token_cost or {}),
                now,
            ),
        )

    conn.commit()
    conn.close()
    return {"id": entry_id, "date": date}


def get_journal_entry(date: str) -> Optional[dict]:
    """Get a single journal entry by date."""
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM journal_entries WHERE date = ?", (date,)
    ).fetchone()
    conn.close()
    return _entry_to_dict(row) if row else None


def get_journal_entries(limit: int = 30, offset: int = 0) -> List[dict]:
    """Get journal entries, most recent first."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM journal_entries ORDER BY date DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    conn.close()
    return [_entry_to_dict(r) for r in rows]


def get_journal_stats() -> dict:
    conn = _get_db()
    total_entries = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
    total_orbs = conn.execute("SELECT COUNT(*) FROM memory_orbs").fetchone()[0]
    core_orbs = conn.execute("SELECT COUNT(*) FROM memory_orbs WHERE is_core = 1").fetchone()[0]
    total_islands = conn.execute("SELECT COUNT(*) FROM islands WHERE status = 'active'").fetchone()[0]

    # Emotion distribution across orbs
    emotion_dist = dict(conn.execute(
        "SELECT emotion, COUNT(*) FROM memory_orbs GROUP BY emotion"
    ).fetchall())

    # Recent emotional tones
    recent_tones = [
        row["emotional_tone"] for row in conn.execute(
            "SELECT emotional_tone FROM journal_entries ORDER BY date DESC LIMIT 7"
        ).fetchall()
    ]

    conn.close()
    return {
        "total_entries": total_entries,
        "total_orbs": total_orbs,
        "core_orbs": core_orbs,
        "total_islands": total_islands,
        "emotion_distribution": emotion_dist,
        "recent_tones": recent_tones,
    }


# --- Memory Orbs ---


def save_memory_orb(
    date: str,
    content: str,
    emotion: str,
    intensity: float = 0.5,
    source_session_id: str = None,
    source_context: str = None,
) -> dict:
    """Save a new memory orb."""
    conn = _get_db()
    orb_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """INSERT INTO memory_orbs
           (id, date, content, emotion, intensity, source_session_id,
            source_context, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (orb_id, date, content, emotion, min(1.0, max(0.0, intensity)),
         source_session_id, source_context, now),
    )
    conn.commit()
    conn.close()
    return {"id": orb_id, "date": date, "emotion": emotion}


def get_memory_orbs(
    limit: int = 50,
    core_only: bool = False,
    date: str = None,
) -> List[dict]:
    """Get memory orbs with optional filters."""
    conn = _get_db()
    query = "SELECT * FROM memory_orbs"
    conditions = []
    params: list = []

    if core_only:
        conditions.append("is_core = 1")
    if date:
        conditions.append("date = ?")
        params.append(date)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY date DESC, intensity DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [_orb_to_dict(r) for r in rows]


def promote_to_core(orb_id: str) -> bool:
    """Promote a memory orb to core memory status."""
    conn = _get_db()
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "UPDATE memory_orbs SET is_core = 1, promoted_at = ? WHERE id = ?",
        (now, orb_id),
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def assign_orb_to_island(orb_id: str, island_id: str) -> bool:
    """Assign a core memory orb to an island."""
    conn = _get_db()
    cursor = conn.execute(
        "UPDATE memory_orbs SET island_id = ? WHERE id = ? AND is_core = 1",
        (island_id, orb_id),
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


# --- Islands ---


def save_island(
    name: str,
    description: str,
    core_memory_ids: list = None,
) -> dict:
    """Create a new island."""
    conn = _get_db()
    island_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """INSERT INTO islands
           (id, name, description, formed_at, last_updated, core_memory_ids)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (island_id, name, description, now, now,
         json.dumps(core_memory_ids or [])),
    )
    conn.commit()

    # Link orbs to this island
    for orb_id in (core_memory_ids or []):
        assign_orb_to_island(orb_id, island_id)

    conn.close()
    return {"id": island_id, "name": name}


def update_island(island_id: str, **kwargs) -> bool:
    """Update island fields. Accepts: description, strength, status, core_memory_ids, narrative."""
    conn = _get_db()
    now = datetime.now(timezone.utc).isoformat()

    updates = ["last_updated = ?"]
    params = [now]

    for field in ("description", "strength", "status", "narrative"):
        if field in kwargs:
            updates.append(f"{field} = ?")
            params.append(kwargs[field])

    if "core_memory_ids" in kwargs:
        updates.append("core_memory_ids = ?")
        params.append(json.dumps(kwargs["core_memory_ids"]))

    params.append(island_id)
    cursor = conn.execute(
        f"UPDATE islands SET {', '.join(updates)} WHERE id = ?", params
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def get_islands(include_fading: bool = False) -> List[dict]:
    """Get all islands."""
    conn = _get_db()
    if include_fading:
        rows = conn.execute(
            "SELECT * FROM islands ORDER BY strength DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM islands WHERE status = 'active' ORDER BY strength DESC"
        ).fetchall()
    conn.close()
    return [_island_to_dict(r) for r in rows]


def get_island(island_id: str) -> Optional[dict]:
    conn = _get_db()
    row = conn.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchone()
    conn.close()
    return _island_to_dict(row) if row else None


# --- Helpers ---


def _entry_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "date": row["date"],
        "content": row["content"],
        "emotional_tone": row["emotional_tone"],
        "secondary_tones": json.loads(row["secondary_tones"]),
        "session_ids": json.loads(row["session_ids"]),
        "model_used": row["model_used"],
        "prompt_version": row["prompt_version"],
        "token_cost": json.loads(row["token_cost"]),
        "created_at": row["created_at"],
    }


def _orb_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "date": row["date"],
        "content": row["content"],
        "emotion": row["emotion"],
        "intensity": row["intensity"],
        "source_session_id": row["source_session_id"],
        "source_context": row["source_context"],
        "is_core": bool(row["is_core"]),
        "promoted_at": row["promoted_at"],
        "island_id": row["island_id"],
        "created_at": row["created_at"],
    }


def _island_to_dict(row: sqlite3.Row) -> dict:
    d = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "formed_at": row["formed_at"],
        "last_updated": row["last_updated"],
        "strength": row["strength"],
        "core_memory_ids": json.loads(row["core_memory_ids"]),
        "status": row["status"],
    }
    try:
        d["narrative"] = row["narrative"] or ""
    except (IndexError, KeyError):
        d["narrative"] = ""
    return d


def _consolidation_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "synthesis": row["synthesis"],
        "patterns": json.loads(row["patterns"]),
        "resolved_contradictions": json.loads(row["resolved_contradictions"]),
        "retired_patterns": json.loads(row["retired_patterns"]),
        "entry_count": row["entry_count"],
        "orb_count": row["orb_count"],
        "created_at": row["created_at"],
    }


# --- Consolidations ---


def save_consolidation(
    period_start: str,
    period_end: str,
    synthesis: str,
    patterns: list = None,
    resolved_contradictions: list = None,
    retired_patterns: list = None,
    entry_count: int = 0,
    orb_count: int = 0,
) -> dict:
    conn = _get_db()
    cid = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO consolidations
           (id, period_start, period_end, synthesis, patterns,
            resolved_contradictions, retired_patterns, entry_count, orb_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cid, period_start, period_end, synthesis,
         json.dumps(patterns or []),
         json.dumps(resolved_contradictions or []),
         json.dumps(retired_patterns or []),
         entry_count, orb_count, now),
    )
    conn.commit()
    conn.close()
    return {"id": cid, "period_start": period_start, "period_end": period_end}


def get_consolidations(limit: int = 10) -> List[dict]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM consolidations ORDER BY period_end DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [_consolidation_to_dict(r) for r in rows]


def get_latest_consolidation() -> Optional[dict]:
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM consolidations ORDER BY period_end DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return _consolidation_to_dict(row) if row else None
