"""Session continuity — detect work threads, blocked items, and suggest resumption."""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

from ..data.session_parser import SessionParser

STOP_WORDS = set(
    "the a an is are was were be been being have has had do does did will would "
    "shall should may might can could i you he she it we they me him her us them "
    "my your his its our their this that these those am in on at to for of with "
    "by from as into through during before after above below between about also "
    "just like would could should need want get got make made let try use using "
    "used work working want going think know see look find file code".split()
)

BLOCKING_PATTERNS = re.compile(
    r"I can'?t|you'?ll need to|requires? (?:you|manual|human)|"
    r"blocked on|waiting for|outside my (?:ability|scope)|"
    r"need you to|can'?t (?:access|reach|connect)|"
    r"not (?:able|possible) (?:to|for me)|"
    r"manual (?:step|intervention|action)|"
    r"beyond (?:what|my)|you (?:should|would need to|will have to)",
    re.IGNORECASE,
)


def _extract_keywords(text: str, top_n: int = 15) -> List[str]:
    """Extract significant keywords from text."""
    tokens = re.findall(r"[a-zA-Z]{4,}", text.lower())
    counts = Counter(t for t in tokens if t not in STOP_WORDS)
    return [w for w, _ in counts.most_common(top_n)]


def detect_work_threads(
    sessions: list,
    parser: SessionParser,
    min_sessions: int = 2,
    min_shared_keywords: int = 3,
) -> List[dict]:
    """Detect work threads — groups of sessions sharing topics."""
    # Extract keywords per session
    session_keywords: Dict[str, Set[str]] = {}
    session_map = {s.session_id: s for s in sessions}

    for s in sessions[:100]:  # Cap for performance
        try:
            messages = parser.get_session_messages(s.session_id, limit=50)
            text = " ".join(m.content or "" for m in messages[:20])
            kws = set(_extract_keywords(text, top_n=20))
            if kws:
                session_keywords[s.session_id] = kws
        except Exception:
            continue

    if not session_keywords:
        return []

    # Group sessions by shared keywords
    # Use greedy clustering: for each session, find others with enough overlap
    used: Set[str] = set()
    threads: List[dict] = []
    thread_id = 0

    sorted_sessions = sorted(
        session_keywords.keys(),
        key=lambda sid: session_map[sid].last_activity if sid in session_map else datetime.min,
        reverse=True,
    )

    for sid in sorted_sessions:
        if sid in used:
            continue

        cluster = [sid]
        cluster_kws = session_keywords[sid].copy()

        for other_sid in sorted_sessions:
            if other_sid == sid or other_sid in used:
                continue
            shared = cluster_kws & session_keywords[other_sid]
            if len(shared) >= min_shared_keywords:
                cluster.append(other_sid)
                cluster_kws |= session_keywords[other_sid]

        if len(cluster) >= min_sessions:
            used.update(cluster)

            # Sort cluster sessions by time
            cluster_sessions = [
                session_map[s] for s in cluster if s in session_map
            ]
            cluster_sessions.sort(key=lambda s: s.last_activity, reverse=True)

            # Compute shared keywords for label
            shared_all = session_keywords[cluster[0]].copy()
            for c in cluster[1:]:
                shared_all &= session_keywords.get(c, set())
            if not shared_all:
                shared_all = set(list(cluster_kws)[:5])

            last_active = cluster_sessions[0].last_activity
            now = datetime.now(timezone.utc)
            last_aware = last_active.replace(tzinfo=timezone.utc) if not last_active.tzinfo else last_active
            days_since = (now - last_aware).days

            status = "active" if days_since < 3 else "stale" if days_since < 14 else "resolved"

            thread_id += 1
            threads.append({
                "thread_id": f"thread-{thread_id}",
                "topic_label": ", ".join(sorted(shared_all)[:5]),
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "title": s.title or s.session_id[:8],
                        "last_activity": s.last_activity.isoformat(),
                        "message_count": s.message_count,
                    }
                    for s in cluster_sessions
                ],
                "session_count": len(cluster_sessions),
                "first_seen": cluster_sessions[-1].start_time.isoformat(),
                "last_seen": cluster_sessions[0].last_activity.isoformat(),
                "status": status,
                "days_since_active": days_since,
            })

    threads.sort(key=lambda t: t["last_seen"], reverse=True)
    return threads


def detect_blocked_items(
    sessions: list,
    parser: SessionParser,
    max_sessions: int = 30,
) -> List[dict]:
    """Find sessions that ended with blocking language."""
    blocked: List[dict] = []

    for s in sessions[:max_sessions]:
        try:
            messages = parser.get_session_messages(s.session_id, limit=200)
            # Check last 5 assistant messages
            assistant_msgs = [m for m in messages if m.type == "assistant"][-5:]

            for msg in assistant_msgs:
                if not msg.content:
                    continue
                match = BLOCKING_PATTERNS.search(msg.content)
                if match:
                    # Extract surrounding sentence
                    start = max(0, msg.content.rfind(".", 0, match.start()) + 1)
                    end = msg.content.find(".", match.end())
                    if end == -1:
                        end = min(len(msg.content), match.end() + 200)
                    snippet = msg.content[start : end + 1].strip()

                    blocked.append({
                        "session_id": s.session_id,
                        "session_title": s.title or s.session_id[:8],
                        "blocked_text": snippet[:300],
                        "timestamp": msg.timestamp.isoformat(),
                    })
                    break  # One per session
        except Exception:
            continue

    return blocked


def get_continuity_summary(sessions: list, parser: SessionParser) -> dict:
    """Full continuity summary — threads, blocked items, suggestions."""
    threads = detect_work_threads(sessions, parser)
    blocked = detect_blocked_items(sessions, parser)

    active = [t for t in threads if t["status"] == "active"]
    stale = [t for t in threads if t["status"] == "stale"]
    resolved = [t for t in threads if t["status"] == "resolved"]

    # Suggested next: most recently active thread
    suggested = active[0] if active else (stale[0] if stale else None)

    # Longest running
    longest = None
    if active:
        longest = min(active, key=lambda t: t["first_seen"])

    return {
        "active_threads": active,
        "stale_threads": stale,
        "resolved_threads": resolved,
        "blocked_items": blocked,
        "suggested_next": suggested,
        "total_open_threads": len(active) + len(stale),
        "longest_running_thread": longest,
    }
