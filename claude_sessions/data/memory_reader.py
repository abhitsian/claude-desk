"""Read and index Claude Code memory files across all projects."""

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FIELD_RE = re.compile(r"^(\w+):\s*(.+)$", re.MULTILINE)


def _parse_frontmatter(text: str) -> dict:
    """Parse YAML-like frontmatter from a markdown file."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    block = m.group(1)
    fields = {}
    for fm in FIELD_RE.finditer(block):
        fields[fm.group(1).strip()] = fm.group(2).strip()
    return fields


def _staleness_days(path: Path) -> int:
    """Days since file was last modified."""
    try:
        mtime = path.stat().st_mtime
        return int((time.time() - mtime) / 86400)
    except OSError:
        return 999


def scan_all_memories() -> List[dict]:
    """Scan all memory directories under ~/.claude/projects/*/memory/."""
    projects_dir = settings.claude_data_dir / "projects"
    memories: List[dict] = []

    if not projects_dir.exists():
        return memories

    # Walk all project memory dirs
    for mem_dir in projects_dir.rglob("memory"):
        if not mem_dir.is_dir():
            continue

        # Derive project name from path
        rel = mem_dir.relative_to(projects_dir)
        project = str(rel.parent)

        for md_file in sorted(mem_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue  # Skip index file

            try:
                content = md_file.read_text(encoding="utf-8")
            except OSError:
                continue

            fm = _parse_frontmatter(content)
            # Strip frontmatter from content for display
            body = FRONTMATTER_RE.sub("", content).strip()
            stale = _staleness_days(md_file)

            memories.append({
                "path": str(md_file),
                "filename": md_file.name,
                "name": fm.get("name", md_file.stem.replace("_", " ").title()),
                "description": fm.get("description", ""),
                "type": fm.get("type", "unknown"),
                "project": project,
                "content": body,
                "modified_date": datetime.fromtimestamp(
                    md_file.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "staleness_days": stale,
                "staleness": "fresh" if stale < 7 else "aging" if stale < 30 else "stale",
            })

    memories.sort(key=lambda m: m["staleness_days"])
    return memories


def get_memory(path: str) -> Optional[dict]:
    """Read a single memory file by path."""
    p = Path(path)
    if not p.exists() or not p.suffix == ".md":
        return None
    content = p.read_text(encoding="utf-8")
    fm = _parse_frontmatter(content)
    body = FRONTMATTER_RE.sub("", content).strip()
    return {
        "path": str(p),
        "name": fm.get("name", p.stem),
        "description": fm.get("description", ""),
        "type": fm.get("type", "unknown"),
        "content": body,
        "frontmatter": fm,
    }


def get_memory_stats() -> dict:
    """Aggregate stats across all memories."""
    memories = scan_all_memories()
    from collections import Counter

    types = Counter(m["type"] for m in memories)
    projects = Counter(m["project"] for m in memories)
    staleness = Counter(m["staleness"] for m in memories)

    return {
        "total": len(memories),
        "by_type": dict(types),
        "by_project": dict(projects),
        "by_staleness": dict(staleness),
        "stale_count": staleness.get("stale", 0),
    }
