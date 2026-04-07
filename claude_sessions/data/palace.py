"""Palace data layer — reads MemPalace structure and provides data for visualization.

Reads from:
1. MemPalace ChromaDB collection (embeddings + metadata)
2. MemPalace config files (wings, identity)
3. MemPalace knowledge graph (SQLite)

Also provides a curation API to ingest from Notion + Claude sessions.
"""

import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MEMPALACE_DIR = Path.home() / ".mempalace"
PALACE_DB = MEMPALACE_DIR / "palace" / "chroma.sqlite3"
KG_DB = MEMPALACE_DIR / "knowledge_graph.db"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def get_identity() -> str:
    path = MEMPALACE_DIR / "identity.txt"
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def get_wing_config() -> dict:
    return _read_json(MEMPALACE_DIR / "wing_config.json")


def get_config() -> dict:
    return _read_json(MEMPALACE_DIR / "config.json")


def get_palace_stats() -> dict:
    """Get overall palace statistics from ChromaDB."""
    try:
        import chromadb
        palace_path = str((MEMPALACE_DIR / "palace").expanduser())
        client = chromadb.PersistentClient(path=palace_path)

        total = 0
        wings: Dict[str, int] = defaultdict(int)
        rooms: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        halls: Dict[str, int] = defaultdict(int)

        for col in client.list_collections():
            count = col.count()
            total += count
            if count > 0:
                # Sample metadata to get wing/room/hall distribution
                results = col.get(limit=min(count, 500), include=["metadatas"])
                for m in results.get("metadatas", []):
                    wing = m.get("wing", "general")
                    room = m.get("room", "general")
                    hall = m.get("hall", "general")
                    wings[wing] += 1
                    rooms[wing][room] += 1
                    halls[hall] += 1

        return {
            "total_drawers": total,
            "wings": dict(wings),
            "rooms": {w: dict(r) for w, r in rooms.items()},
            "halls": dict(halls),
            "collections": len(client.list_collections()),
        }
    except Exception:
        return {"total_drawers": 0, "wings": {}, "rooms": {}, "halls": {}, "collections": 0}


def get_palace_from_cli() -> dict:
    """Fallback: get palace data by running CLI status command."""
    import subprocess
    try:
        result = subprocess.run(
            ["python3", "-m", "mempalace.cli", "status"],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout + result.stderr

        # Parse the status output
        wings: Dict[str, Dict[str, int]] = {}
        current_wing = None
        total = 0

        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("WING:"):
                current_wing = line.split("WING:")[1].strip()
                wings[current_wing] = {}
            elif line.startswith("ROOM:") and current_wing:
                parts = line.split()
                room_name = parts[1] if len(parts) > 1 else "unknown"
                count = 0
                for p in parts:
                    if p.isdigit():
                        count = int(p)
                wings[current_wing][room_name] = count
                total += count

        return {
            "total_drawers": total,
            "wings": {w: sum(r.values()) for w, r in wings.items()},
            "rooms": wings,
            "halls": {},
        }
    except Exception:
        return {"total_drawers": 0, "wings": {}, "rooms": {}, "halls": {}}


def search_palace(query: str, wing: str = "", room: str = "", limit: int = 10) -> List[dict]:
    """Search the palace using mempalace's searcher."""
    try:
        from mempalace.searcher import search_memories
        palace_path = str((MEMPALACE_DIR / "palace").expanduser())

        results = search_memories(
            query,
            palace_path=palace_path,
            n_results=limit,
        )
        return results if isinstance(results, list) else []
    except Exception:
        # Fallback to CLI
        import subprocess
        try:
            cmd = ["python3", "-m", "mempalace.cli", "search", query]
            if wing:
                cmd.extend(["--wing", wing])
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            # Parse output into structured results
            entries = []
            current: Dict[str, Any] = {}
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("[") and "]" in line:
                    if current:
                        entries.append(current)
                    # Parse [1] wing / room
                    parts = line.split("]", 1)
                    location = parts[1].strip() if len(parts) > 1 else ""
                    wing_room = location.split("/")
                    current = {
                        "wing": wing_room[0].strip() if wing_room else "",
                        "room": wing_room[1].strip() if len(wing_room) > 1 else "",
                        "content": "",
                    }
                elif line.startswith("Source:") and current:
                    current["source"] = line.split("Source:")[1].strip()
                elif line.startswith("Match:") and current:
                    try:
                        current["score"] = float(line.split("Match:")[1].strip())
                    except ValueError:
                        pass
                elif line and current and not line.startswith("─"):
                    current["content"] = line
            if current:
                entries.append(current)
            return entries[:limit]
        except Exception:
            return []


def get_knowledge_graph_stats() -> dict:
    """Get knowledge graph statistics if available."""
    if not KG_DB.exists():
        return {"available": False}

    try:
        conn = sqlite3.connect(str(KG_DB))
        conn.row_factory = sqlite3.Row

        total = conn.execute("SELECT COUNT(*) FROM triples").fetchone()[0]
        entities = conn.execute("SELECT COUNT(DISTINCT subject) FROM triples").fetchone()[0]
        relations = conn.execute(
            "SELECT predicate, COUNT(*) as c FROM triples GROUP BY predicate ORDER BY c DESC LIMIT 10"
        ).fetchall()

        conn.close()
        return {
            "available": True,
            "total_triples": total,
            "unique_entities": entities,
            "top_relations": [{"predicate": r["predicate"], "count": r["c"]} for r in relations],
        }
    except Exception:
        return {"available": False}


def get_full_palace_view() -> dict:
    """Assemble everything for the palace visualization page."""
    # Try ChromaDB first, fall back to CLI
    stats = get_palace_stats()
    if stats["total_drawers"] == 0:
        stats = get_palace_from_cli()

    wing_config = get_wing_config()
    identity = get_identity()
    kg = get_knowledge_graph_stats()

    # Enrich wings with config metadata
    wing_details = []
    config_wings = wing_config.get("wings", {})
    for wing_name, drawer_count in stats.get("wings", {}).items():
        cfg = config_wings.get(wing_name, {})
        rooms_data = stats.get("rooms", {}).get(wing_name, {})
        wing_details.append({
            "name": wing_name,
            "display_name": wing_name.replace("wing_", "").replace("_", " ").title(),
            "type": cfg.get("type", "unknown"),
            "keywords": cfg.get("keywords", []),
            "drawer_count": drawer_count,
            "rooms": [
                {"name": r, "count": c}
                for r, c in sorted(rooms_data.items(), key=lambda x: -x[1])
            ],
            "room_count": len(rooms_data),
        })

    wing_details.sort(key=lambda w: -w["drawer_count"])

    return {
        "identity": identity,
        "total_drawers": stats.get("total_drawers", 0),
        "wings": wing_details,
        "wing_count": len(wing_details),
        "halls": stats.get("halls", {}),
        "knowledge_graph": kg,
        "config": get_config(),
    }
