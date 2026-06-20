"""Persistent dimensional profiles — observations, profiles, and trajectory tracking."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from ..config import settings

DB_PATH = settings.claude_data_dir / "dimensions.db"

VALID_DIMENSIONS = {"taste", "reasoning", "energy", "tempo", "language"}


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dimension_observations (
            id TEXT PRIMARY KEY,
            dimension TEXT NOT NULL,
            date TEXT NOT NULL,
            session_id TEXT,
            data TEXT NOT NULL,
            signal_count INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_obs_dimension ON dimension_observations(dimension);
        CREATE INDEX IF NOT EXISTS idx_obs_date ON dimension_observations(date);
        CREATE INDEX IF NOT EXISTS idx_obs_session ON dimension_observations(session_id);

        CREATE TABLE IF NOT EXISTS dimension_profiles (
            id TEXT PRIMARY KEY,
            dimension TEXT NOT NULL UNIQUE,
            profile TEXT NOT NULL,
            summary TEXT NOT NULL,
            trajectory TEXT DEFAULT '',
            confidence REAL DEFAULT 0.5,
            observation_count INTEGER DEFAULT 0,
            last_observation_date TEXT,
            last_consolidated_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dimension_history (
            id TEXT PRIMARY KEY,
            dimension TEXT NOT NULL,
            profile_snapshot TEXT NOT NULL,
            summary TEXT NOT NULL,
            observation_count INTEGER DEFAULT 0,
            trigger TEXT DEFAULT 'consolidation',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_history_dimension ON dimension_history(dimension);
        CREATE INDEX IF NOT EXISTS idx_history_created ON dimension_history(created_at);
    """)
    conn.commit()
    return conn


# --- Observations ---


def save_observation(
    dimension: str,
    date: str,
    data: dict,
    session_id: str = None,
    signal_count: int = 1,
) -> dict:
    conn = _get_db()
    obs_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO dimension_observations
           (id, dimension, date, session_id, data, signal_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (obs_id, dimension, date, session_id, json.dumps(data, default=str), signal_count, now),
    )
    conn.commit()
    conn.close()
    return {"id": obs_id, "dimension": dimension, "date": date}


def get_observations(
    dimension: str,
    limit: int = 50,
    since_date: str = None,
) -> List[dict]:
    conn = _get_db()
    if since_date:
        rows = conn.execute(
            "SELECT * FROM dimension_observations WHERE dimension = ? AND date >= ? ORDER BY date DESC LIMIT ?",
            (dimension, since_date, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM dimension_observations WHERE dimension = ? ORDER BY date DESC LIMIT ?",
            (dimension, limit),
        ).fetchall()
    conn.close()
    return [_obs_to_dict(r) for r in rows]


def get_observations_since_last_consolidation(dimension: str) -> List[dict]:
    conn = _get_db()
    profile = conn.execute(
        "SELECT last_consolidated_at FROM dimension_profiles WHERE dimension = ?",
        (dimension,),
    ).fetchone()

    if profile and profile["last_consolidated_at"]:
        since = profile["last_consolidated_at"][:10]  # date part only
        rows = conn.execute(
            "SELECT * FROM dimension_observations WHERE dimension = ? AND date > ? ORDER BY date ASC",
            (dimension, since),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM dimension_observations WHERE dimension = ? ORDER BY date ASC",
            (dimension,),
        ).fetchall()

    conn.close()
    return [_obs_to_dict(r) for r in rows]


def has_observations_for_date(dimension: str, date: str) -> bool:
    conn = _get_db()
    row = conn.execute(
        "SELECT 1 FROM dimension_observations WHERE dimension = ? AND date = ? LIMIT 1",
        (dimension, date),
    ).fetchone()
    conn.close()
    return row is not None


# --- Profiles ---


def upsert_profile(
    dimension: str,
    profile: dict,
    summary: str,
    trajectory: str = "",
    confidence: float = 0.5,
    observation_count: int = 0,
) -> dict:
    conn = _get_db()
    now = datetime.now(timezone.utc).isoformat()

    existing = conn.execute(
        "SELECT id FROM dimension_profiles WHERE dimension = ?", (dimension,)
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE dimension_profiles
               SET profile = ?, summary = ?, trajectory = ?, confidence = ?,
                   observation_count = ?, last_consolidated_at = ?, updated_at = ?
               WHERE dimension = ?""",
            (json.dumps(profile, default=str), summary, trajectory,
             confidence, observation_count, now, now, dimension),
        )
        pid = existing["id"]
    else:
        pid = str(uuid.uuid4())[:8]
        conn.execute(
            """INSERT INTO dimension_profiles
               (id, dimension, profile, summary, trajectory, confidence,
                observation_count, last_consolidated_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pid, dimension, json.dumps(profile, default=str), summary, trajectory,
             confidence, observation_count, now, now, now),
        )

    conn.commit()
    conn.close()
    return {"id": pid, "dimension": dimension}


def get_profile(dimension: str) -> Optional[dict]:
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM dimension_profiles WHERE dimension = ?", (dimension,)
    ).fetchone()
    conn.close()
    return _profile_to_dict(row) if row else None


def get_all_profiles() -> List[dict]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM dimension_profiles ORDER BY dimension"
    ).fetchall()
    conn.close()
    return [_profile_to_dict(r) for r in rows]


# --- History ---


def save_history_snapshot(
    dimension: str,
    profile_snapshot: dict,
    summary: str,
    observation_count: int = 0,
    trigger: str = "consolidation",
) -> dict:
    conn = _get_db()
    hid = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO dimension_history
           (id, dimension, profile_snapshot, summary, observation_count, trigger, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (hid, dimension, json.dumps(profile_snapshot, default=str),
         summary, observation_count, trigger, now),
    )
    conn.commit()
    conn.close()
    return {"id": hid}


def get_history(dimension: str, limit: int = 10) -> List[dict]:
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM dimension_history WHERE dimension = ? ORDER BY created_at DESC LIMIT ?",
        (dimension, limit),
    ).fetchall()
    conn.close()
    return [_history_to_dict(r) for r in rows]


# --- Stats ---


def get_dimension_stats() -> dict:
    conn = _get_db()
    total_obs = conn.execute("SELECT COUNT(*) FROM dimension_observations").fetchone()[0]
    obs_by_dim = dict(conn.execute(
        "SELECT dimension, COUNT(*) FROM dimension_observations GROUP BY dimension"
    ).fetchall())
    profiles_count = conn.execute("SELECT COUNT(*) FROM dimension_profiles").fetchone()[0]
    history_count = conn.execute("SELECT COUNT(*) FROM dimension_history").fetchone()[0]
    conn.close()
    return {
        "total_observations": total_obs,
        "observations_by_dimension": obs_by_dim,
        "profiles_count": profiles_count,
        "history_snapshots": history_count,
    }


# --- Helpers ---


def _obs_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "dimension": row["dimension"],
        "date": row["date"],
        "session_id": row["session_id"],
        "data": json.loads(row["data"]),
        "signal_count": row["signal_count"],
        "created_at": row["created_at"],
    }


def _profile_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "dimension": row["dimension"],
        "profile": json.loads(row["profile"]),
        "summary": row["summary"],
        "trajectory": row["trajectory"],
        "confidence": row["confidence"],
        "observation_count": row["observation_count"],
        "last_observation_date": row["last_observation_date"],
        "last_consolidated_at": row["last_consolidated_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _history_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "dimension": row["dimension"],
        "profile_snapshot": json.loads(row["profile_snapshot"]),
        "summary": row["summary"],
        "observation_count": row["observation_count"],
        "trigger": row["trigger"],
        "created_at": row["created_at"],
    }
