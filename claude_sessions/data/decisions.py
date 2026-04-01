"""Persistent decision log — extract, store, and manage decisions from sessions."""

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from ..config import settings
from ..data.session_parser import SessionParser

DB_PATH = settings.claude_data_dir / "decisions.db"

DECISION_PATTERNS = [
    (r"(?:I (?:recommend|suggest|advise|propose))\s+(?:that you|you should|to)", "recommendation"),
    (r"(?:you should|I(?:'d| would) (?:recommend|suggest))\s+(?:stay|leave|move|switch|focus|go with|choose|pick|use|avoid)", "direction"),
    (r"(?:the answer|conclusion|bottom line|net-net|in summary|to summarize)\s*(?:is|:|—)", "conclusion"),
    (r"(?:decision|decided|deciding|let'?s go with|we'?ll go with)\s*(?:to|:|that|—)", "decision"),
    (r"(?:trade-?off|on (?:one|the other) hand|versus|the (?:downside|upside|risk)|weighing|balance between)", "tradeoff"),
    (r"(?:best (?:approach|option|path|choice|strategy)|the way to go|optimal)", "recommendation"),
    (r"(?:don'?t|avoid|stay away from|skip|not worth)\s+\w+ing", "direction"),
]

COMPILED_PATTERNS = [(re.compile(p, re.IGNORECASE), t) for p, t in DECISION_PATTERNS]


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            decision_text TEXT NOT NULL,
            context TEXT DEFAULT '',
            decision_type TEXT DEFAULT 'decision',
            timestamp TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            linked_sessions TEXT DEFAULT '[]',
            extracted_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_decisions_session
        ON decisions(session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_decisions_status
        ON decisions(status)
    """)
    conn.commit()
    return conn


def _is_extracted(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM decisions WHERE session_id = ? LIMIT 1", (session_id,)
    ).fetchone()
    return row is not None


def extract_and_store(session_id: str, parser: SessionParser) -> int:
    """Extract decisions from a session and store them. Returns count of new decisions."""
    conn = _get_db()
    if _is_extracted(conn, session_id):
        conn.close()
        return 0

    messages = parser.get_session_messages(session_id, limit=2000)
    now = datetime.now(timezone.utc).isoformat()
    decisions_found: List[dict] = []

    for msg in messages:
        if msg.type != "assistant" or not msg.content:
            continue

        content = msg.content
        for pattern, decision_type in COMPILED_PATTERNS:
            for match in pattern.finditer(content):
                # Extract the sentence
                start = max(0, content.rfind(".", 0, match.start()) + 1)
                end = content.find(".", match.end())
                if end == -1:
                    end = min(len(content), match.end() + 250)
                else:
                    end += 1

                snippet = content[start:end].strip()
                if len(snippet) < 30:
                    continue

                # Extract paragraph for context
                para_start = max(0, content.rfind("\n\n", 0, match.start()))
                para_end = content.find("\n\n", match.end())
                if para_end == -1:
                    para_end = min(len(content), match.end() + 500)
                context = content[para_start:para_end].strip()

                decisions_found.append({
                    "decision_text": snippet[:400],
                    "context": context[:800],
                    "decision_type": decision_type,
                    "timestamp": msg.timestamp.isoformat(),
                })

    # Deduplicate similar decisions
    seen = set()
    unique = []
    for d in decisions_found:
        key = d["decision_text"][:60].lower()
        if key not in seen:
            seen.add(key)
            unique.append(d)

    # Store (cap at 15 per session)
    for d in unique[:15]:
        conn.execute(
            """INSERT INTO decisions
               (session_id, decision_text, context, decision_type, timestamp, extracted_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, d["decision_text"], d["context"],
             d["decision_type"], d["timestamp"], now),
        )

    conn.commit()
    conn.close()
    return len(unique[:15])


def get_all_decisions(
    status: Optional[str] = None,
    decision_type: Optional[str] = None,
    limit: int = 100,
) -> List[dict]:
    conn = _get_db()
    query = "SELECT * FROM decisions"
    params: list = []
    conditions = []

    if status:
        conditions.append("status = ?")
        params.append(status)
    if decision_type:
        conditions.append("decision_type = ?")
        params.append(decision_type)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_decision(decision_id: int) -> Optional[dict]:
    conn = _get_db()
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    conn.close()
    return _row_to_dict(row) if row else None


def update_status(decision_id: int, new_status: str) -> bool:
    if new_status not in ("active", "superseded", "reversed"):
        return False
    conn = _get_db()
    cursor = conn.execute(
        "UPDATE decisions SET status = ? WHERE id = ?", (new_status, decision_id)
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def search_decisions(query: str, limit: int = 50) -> List[dict]:
    conn = _get_db()
    rows = conn.execute(
        """SELECT * FROM decisions
           WHERE decision_text LIKE ? OR context LIKE ?
           ORDER BY timestamp DESC LIMIT ?""",
        (f"%{query}%", f"%{query}%", limit),
    ).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


def get_decision_stats() -> dict:
    conn = _get_db()
    total = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    by_type = dict(conn.execute(
        "SELECT decision_type, COUNT(*) FROM decisions GROUP BY decision_type"
    ).fetchall())
    by_status = dict(conn.execute(
        "SELECT status, COUNT(*) FROM decisions GROUP BY status"
    ).fetchall())
    conn.close()
    return {"total": total, "by_type": by_type, "by_status": by_status}


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "decision_text": row["decision_text"],
        "context": row["context"],
        "decision_type": row["decision_type"],
        "timestamp": row["timestamp"],
        "status": row["status"],
        "linked_sessions": json.loads(row["linked_sessions"]),
        "extracted_at": row["extracted_at"],
    }
