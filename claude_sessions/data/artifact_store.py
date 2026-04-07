"""Artifact store — versioned, filtered, deduplicated artifact tracking.

Groups all Write/Edit operations on the same file path into a single
artifact with a version timeline. Filters out noise (config, temp,
memory files, intermediate tool scaffolding) and keeps only concrete
deliverables: apps, pages, documents, images, scripts, data.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import settings

DB_PATH = settings.claude_data_dir / "artifact-store.db"

# ── Noise filters ──────────────────────────────────────────────

EXCLUDE_PATH_PATTERNS = [
    re.compile(r"\.claude/"),          # Claude config/memory/settings
    re.compile(r"/\."),                # Dotfiles (.gitignore, .env, etc.)
    re.compile(r"/tmp/"),              # Temp files
    re.compile(r"node_modules/"),      # Dependencies
    re.compile(r"__pycache__/"),       # Python cache
    re.compile(r"\.git/"),             # Git internals
    re.compile(r"package-lock\.json"), # Lock files
    re.compile(r"bun\.lockb"),
    re.compile(r"yarn\.lock"),
    re.compile(r"Pipfile\.lock"),
]

# Only track concrete output types
ARTIFACT_EXTENSIONS = {
    # Apps & Pages
    ".html": "app",
    ".vue": "app",
    ".svelte": "app",
    ".jsx": "app",
    ".tsx": "app",
    # Documents
    ".md": "document",
    ".txt": "document",
    ".rst": "document",
    # Scripts & Tools
    ".py": "script",
    ".js": "script",
    ".ts": "script",
    ".sh": "script",
    ".bash": "script",
    ".rb": "script",
    ".go": "script",
    ".rs": "script",
    # Styles
    ".css": "app",
    ".scss": "app",
    # Data
    ".csv": "data",
    ".json": "data",
    ".yaml": "data",
    ".yml": "data",
    ".sql": "data",
    ".xml": "data",
    # Images
    ".svg": "image",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
}

# For .md files, exclude if path looks like internal scaffolding
MD_EXCLUDE_PATTERNS = [
    re.compile(r"CLAUDE\.md", re.I),
    re.compile(r"MEMORY\.md", re.I),
    re.compile(r"SKILL\.md", re.I),
    re.compile(r"README\.md", re.I),
    re.compile(r"CHANGELOG", re.I),
    re.compile(r"/commands/"),
    re.compile(r"/skills/"),
    re.compile(r"/memory/"),
]

# For .json files, exclude if it's config
JSON_EXCLUDE_PATTERNS = [
    re.compile(r"settings\.json"),
    re.compile(r"package\.json"),
    re.compile(r"tsconfig"),
    re.compile(r"\.eslint"),
    re.compile(r"\.prettier"),
]


def _is_concrete_artifact(file_path: str) -> bool:
    """Determine if a file path represents a concrete deliverable."""
    ext = Path(file_path).suffix.lower()

    # Must have a recognized extension
    if ext not in ARTIFACT_EXTENSIONS:
        return False

    # Must not match exclude patterns
    for pattern in EXCLUDE_PATH_PATTERNS:
        if pattern.search(file_path):
            return False

    # Extra filtering for .md files
    if ext == ".md":
        for pattern in MD_EXCLUDE_PATTERNS:
            if pattern.search(file_path):
                return False

    # Extra filtering for .json files
    if ext == ".json":
        for pattern in JSON_EXCLUDE_PATTERNS:
            if pattern.search(file_path):
                return False

    return True


def _get_artifact_type(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    return ARTIFACT_EXTENSIONS.get(ext, "other")


def _derive_title(file_path: str) -> str:
    """Derive a human-readable title from a file path."""
    name = Path(file_path).stem
    # Convert snake_case and kebab-case to Title Case
    name = name.replace("_", " ").replace("-", " ")
    return name.title()


# ── Database ───────────────────────────────────────────────────


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            extension TEXT NOT NULL,
            current_size_bytes INTEGER DEFAULT 0,
            exists_on_disk INTEGER DEFAULT 1,
            first_seen TEXT NOT NULL,
            last_modified TEXT NOT NULL,
            version_count INTEGER DEFAULT 1,
            sessions_involved TEXT DEFAULT '[]',
            project_path TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS artifact_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artifact_id INTEGER NOT NULL,
            version_number INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            size_bytes INTEGER DEFAULT 0,
            content_preview TEXT DEFAULT '',
            change_summary TEXT DEFAULT '',
            FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
        );

        CREATE INDEX IF NOT EXISTS idx_artifact_path ON artifacts(file_path);
        CREATE INDEX IF NOT EXISTS idx_versions_artifact ON artifact_versions(artifact_id);
        CREATE INDEX IF NOT EXISTS idx_versions_session ON artifact_versions(session_id);
    """)
    conn.commit()
    return conn


# ── Ingestion ──────────────────────────────────────────────────


def ingest_session(session_id: str, session_file: Path) -> int:
    """Scan a JSONL session file and ingest artifacts + versions.

    Returns count of new versions added.
    """
    conn = _get_db()
    new_versions = 0

    # Track all file operations in order
    operations: List[Dict[str, Any]] = []

    with open(session_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Parse tool_use blocks from assistant messages
            message = obj.get("message", {})
            content = message.get("content", [])
            if not isinstance(content, list):
                continue

            timestamp_str = obj.get("timestamp", "")

            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue

                tool_name = block.get("name", "")
                tool_input = block.get("input", {})
                if not isinstance(tool_input, dict):
                    continue

                if tool_name not in ("Write", "Edit"):
                    continue

                file_path = tool_input.get("file_path", "")
                if not file_path or not _is_concrete_artifact(file_path):
                    continue

                file_content = tool_input.get("content", "")
                old_string = tool_input.get("old_string", "")
                new_string = tool_input.get("new_string", "")

                operation = "create" if tool_name == "Write" else "edit"

                # Generate change summary for edits
                change_summary = ""
                if operation == "edit" and old_string and new_string:
                    old_preview = old_string[:60].replace("\n", " ")
                    new_preview = new_string[:60].replace("\n", " ")
                    change_summary = f'"{old_preview}" → "{new_preview}"'

                # Content preview (first 200 chars for writes)
                content_preview = ""
                if file_content:
                    content_preview = file_content[:200].replace("\n", " ")

                size_bytes = len(file_content) if file_content else 0
                if operation == "edit":
                    size_bytes = len(new_string) - len(old_string)  # Delta

                operations.append({
                    "file_path": file_path,
                    "operation": operation,
                    "timestamp": timestamp_str,
                    "size_bytes": size_bytes,
                    "content_preview": content_preview,
                    "change_summary": change_summary,
                })

            # Also check toolUseResult blocks
            tool_result = obj.get("toolUseResult")
            if tool_result and isinstance(tool_result, dict):
                file_path = tool_result.get("filePath", "")
                if file_path and _is_concrete_artifact(file_path):
                    content = tool_result.get("content", "")
                    op_type = tool_result.get("type", "unknown")
                    operation = "create" if op_type in ("create", "write") else "edit"

                    # Avoid duplicate if we already got this from tool_use
                    already_seen = any(
                        o["file_path"] == file_path and o["timestamp"] == timestamp_str
                        for o in operations
                    )
                    if not already_seen:
                        operations.append({
                            "file_path": file_path,
                            "operation": operation,
                            "timestamp": timestamp_str,
                            "size_bytes": len(content) if content else 0,
                            "content_preview": (content[:200].replace("\n", " ")) if content else "",
                            "change_summary": "",
                        })

    # Now store into DB
    for op in operations:
        try:
            new_versions += _upsert_artifact_version(conn, session_id, op)
        except Exception:
            continue

    conn.commit()
    conn.close()
    return new_versions


def _upsert_artifact_version(
    conn: sqlite3.Connection,
    session_id: str,
    op: Dict[str, Any],
) -> int:
    """Insert or update artifact and add a version. Returns 1 if new version added."""
    file_path = op["file_path"]

    # Check if artifact exists
    row = conn.execute(
        "SELECT id, version_count, sessions_involved FROM artifacts WHERE file_path = ?",
        (file_path,),
    ).fetchone()

    # Check for duplicate version (same session + same timestamp)
    if row:
        existing = conn.execute(
            "SELECT 1 FROM artifact_versions WHERE artifact_id = ? AND session_id = ? AND timestamp = ?",
            (row["id"], session_id, op["timestamp"]),
        ).fetchone()
        if existing:
            return 0

    now = op["timestamp"] or datetime.now(timezone.utc).isoformat()
    exists = Path(file_path).exists()

    if row:
        artifact_id = row["id"]
        new_version = row["version_count"] + 1
        sessions = json.loads(row["sessions_involved"])
        if session_id not in sessions:
            sessions.append(session_id)

        # Get current file size
        current_size = 0
        if exists:
            try:
                current_size = Path(file_path).stat().st_size
            except OSError:
                pass

        conn.execute(
            """UPDATE artifacts SET
                version_count = ?, last_modified = ?, sessions_involved = ?,
                exists_on_disk = ?, current_size_bytes = ?
               WHERE id = ?""",
            (new_version, now, json.dumps(sessions), int(exists), current_size, artifact_id),
        )
    else:
        new_version = 1
        current_size = 0
        if exists:
            try:
                current_size = Path(file_path).stat().st_size
            except OSError:
                pass

        # Derive project from path
        project = ""
        parts = file_path.split("/")
        # Use the directory name closest to the file as project hint
        if len(parts) >= 2:
            project = parts[-2] if parts[-2] != "vaibhav" else (parts[-3] if len(parts) >= 3 else "")

        cursor = conn.execute(
            """INSERT INTO artifacts
               (file_path, title, artifact_type, extension, current_size_bytes,
                exists_on_disk, first_seen, last_modified, version_count,
                sessions_involved, project_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                file_path,
                _derive_title(file_path),
                _get_artifact_type(file_path),
                Path(file_path).suffix.lower(),
                current_size,
                int(exists),
                now,
                now,
                json.dumps([session_id]),
                project,
            ),
        )
        artifact_id = cursor.lastrowid

    # Add version
    conn.execute(
        """INSERT INTO artifact_versions
           (artifact_id, version_number, session_id, operation, timestamp,
            size_bytes, content_preview, change_summary)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            artifact_id,
            new_version,
            session_id,
            op["operation"],
            now,
            op["size_bytes"],
            op["content_preview"][:200],
            op["change_summary"][:300],
        ),
    )
    return 1


# ── Queries ────────────────────────────────────────────────────


def ingest_all_sessions() -> Dict[str, int]:
    """Scan all JSONL files and ingest artifacts. Idempotent."""
    projects_dir = settings.claude_data_dir / "projects"
    total_artifacts = 0
    total_versions = 0
    sessions_scanned = 0

    if not projects_dir.exists():
        return {"artifacts": 0, "versions": 0, "sessions": 0}

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for session_file in project_dir.glob("*.jsonl"):
            if session_file.stem.startswith("agent-"):
                continue
            sessions_scanned += 1
            try:
                v = ingest_session(session_file.stem, session_file)
                total_versions += v
            except Exception:
                continue

    conn = _get_db()
    total_artifacts = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    conn.close()

    return {
        "artifacts": total_artifacts,
        "versions": total_versions,
        "sessions": sessions_scanned,
    }


def get_all_artifacts(
    artifact_type: Optional[str] = None,
    limit: int = 200,
) -> List[dict]:
    """Get all artifacts, optionally filtered by type."""
    conn = _get_db()
    query = "SELECT * FROM artifacts"
    params: list = []

    if artifact_type:
        query += " WHERE artifact_type = ?"
        params.append(artifact_type)

    query += " ORDER BY last_modified DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "file_path": r["file_path"],
            "file_name": Path(r["file_path"]).name,
            "title": r["title"],
            "artifact_type": r["artifact_type"],
            "extension": r["extension"],
            "current_size_bytes": r["current_size_bytes"],
            "exists_on_disk": bool(r["exists_on_disk"]),
            "first_seen": r["first_seen"],
            "last_modified": r["last_modified"],
            "version_count": r["version_count"],
            "sessions_involved": json.loads(r["sessions_involved"]),
            "project_path": r["project_path"],
        }
        for r in rows
    ]


def get_artifact_detail(artifact_id: int) -> Optional[dict]:
    """Get a single artifact with all versions."""
    conn = _get_db()
    row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if not row:
        conn.close()
        return None

    versions = conn.execute(
        "SELECT * FROM artifact_versions WHERE artifact_id = ? ORDER BY version_number DESC",
        (artifact_id,),
    ).fetchall()
    conn.close()

    # Try to read current content from disk
    content = None
    file_path = row["file_path"]
    if Path(file_path).exists():
        try:
            ext = Path(file_path).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                content = Path(file_path).read_text(encoding="utf-8")[:50000]
        except Exception:
            pass

    return {
        "id": row["id"],
        "file_path": row["file_path"],
        "file_name": Path(row["file_path"]).name,
        "title": row["title"],
        "artifact_type": row["artifact_type"],
        "extension": row["extension"],
        "current_size_bytes": row["current_size_bytes"],
        "exists_on_disk": bool(row["exists_on_disk"]),
        "first_seen": row["first_seen"],
        "last_modified": row["last_modified"],
        "version_count": row["version_count"],
        "sessions_involved": json.loads(row["sessions_involved"]),
        "project_path": row["project_path"],
        "content": content,
        "versions": [
            {
                "version_number": v["version_number"],
                "session_id": v["session_id"],
                "operation": v["operation"],
                "timestamp": v["timestamp"],
                "size_bytes": v["size_bytes"],
                "content_preview": v["content_preview"],
                "change_summary": v["change_summary"],
            }
            for v in versions
        ],
    }


def get_artifact_by_path(file_path: str) -> Optional[dict]:
    """Look up an artifact by its file path."""
    conn = _get_db()
    row = conn.execute("SELECT id FROM artifacts WHERE file_path = ?", (file_path,)).fetchone()
    conn.close()
    if not row:
        return None
    return get_artifact_detail(row["id"])


def get_artifact_stats() -> dict:
    """Aggregate stats about all artifacts."""
    conn = _get_db()
    total = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    by_type = dict(conn.execute(
        "SELECT artifact_type, COUNT(*) FROM artifacts GROUP BY artifact_type"
    ).fetchall())
    total_versions = conn.execute("SELECT COUNT(*) FROM artifact_versions").fetchone()[0]
    multi_version = conn.execute(
        "SELECT COUNT(*) FROM artifacts WHERE version_count > 1"
    ).fetchone()[0]
    total_size = conn.execute(
        "SELECT SUM(current_size_bytes) FROM artifacts WHERE exists_on_disk = 1"
    ).fetchone()[0] or 0
    on_disk = conn.execute(
        "SELECT COUNT(*) FROM artifacts WHERE exists_on_disk = 1"
    ).fetchone()[0]
    conn.close()

    return {
        "total": total,
        "by_type": by_type,
        "total_versions": total_versions,
        "multi_version": multi_version,
        "total_size_bytes": total_size,
        "on_disk": on_disk,
        "deleted": total - on_disk,
    }


def search_artifacts(query: str, limit: int = 50) -> List[dict]:
    """Search artifacts by title or file path."""
    conn = _get_db()
    rows = conn.execute(
        """SELECT * FROM artifacts
           WHERE title LIKE ? OR file_path LIKE ?
           ORDER BY last_modified DESC LIMIT ?""",
        (f"%{query}%", f"%{query}%", limit),
    ).fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "file_path": r["file_path"],
            "file_name": Path(r["file_path"]).name,
            "title": r["title"],
            "artifact_type": r["artifact_type"],
            "version_count": r["version_count"],
            "last_modified": r["last_modified"],
            "exists_on_disk": bool(r["exists_on_disk"]),
        }
        for r in rows
    ]


def update_title(artifact_id: int, title: str) -> bool:
    """Allow user to rename an artifact."""
    conn = _get_db()
    cursor = conn.execute("UPDATE artifacts SET title = ? WHERE id = ?", (title, artifact_id))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0
