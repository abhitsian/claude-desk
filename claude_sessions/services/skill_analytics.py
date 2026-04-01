"""Skill usage analytics across sessions.

Detects skill invocations from conversation messages and computes
usage statistics, chains, and session-length correlations.
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ..data.models import ConversationMessage
from ..data.session_parser import SessionParser

# Patterns that indicate a skill invocation
SKILL_PATTERNS = [
    re.compile(r"^/(\w[\w-]*)"),  # /skill-name at start of message
    re.compile(r"<command-name>(\w[\w-]*)</command-name>"),  # XML tag
]

TOOL_SKILL_NAMES = {"Skill", "SkillTool"}


def extract_skill_usage(session_id: str, parser: SessionParser) -> List[dict]:
    """Extract skill invocations from a session's messages."""
    messages = parser.get_session_messages(session_id, limit=2000)
    usages: List[dict] = []

    for msg in messages:
        skills_found: List[str] = []

        # Check user messages for /slash-command patterns
        if msg.type == "user" and msg.content:
            for pattern in SKILL_PATTERNS:
                m = pattern.search(msg.content.strip())
                if m:
                    skills_found.append(m.group(1))

        # Check assistant tool calls for Skill tool invocations
        if msg.type == "assistant":
            for tc in msg.tool_calls:
                tool_name = tc.get("name") or tc.get("tool_name") or ""
                if tool_name in TOOL_SKILL_NAMES:
                    inp = tc.get("input") or tc.get("tool_input") or {}
                    skill_name = inp.get("skill") or inp.get("name") or ""
                    if skill_name:
                        skills_found.append(skill_name)

            for td in msg.tool_details:
                if td.name in TOOL_SKILL_NAMES:
                    if td.input_summary:
                        # Try to extract skill name from summary
                        m = re.search(r'skill[=:]\s*["\']?(\w[\w-]*)', td.input_summary, re.I)
                        if m:
                            skills_found.append(m.group(1))

        for skill in skills_found:
            # Skip built-in CLI commands
            if skill in ("help", "clear", "compact", "cost", "doctor", "hooks",
                         "config", "status", "exit", "quit", "model", "vim",
                         "permissions", "mcp", "agents", "memory"):
                continue
            usages.append({
                "skill_name": skill,
                "timestamp": msg.timestamp.isoformat(),
                "session_id": session_id,
            })

    return usages


def get_skill_stats(
    sessions: list,
    parser: SessionParser,
    days: int = 30,
) -> dict:
    """Compute skill usage statistics over a time period."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    period_sessions = [
        s for s in sessions
        if s.start_time.replace(tzinfo=timezone.utc) > cutoff
    ]

    all_usages: List[dict] = []
    session_skills: Dict[str, List[str]] = {}  # session_id -> [skill_names]

    for s in period_sessions:
        try:
            usages = extract_skill_usage(s.session_id, parser)
            all_usages.extend(usages)
            session_skills[s.session_id] = [u["skill_name"] for u in usages]
        except Exception:
            continue

    # Skill counts
    skill_counts = Counter(u["skill_name"] for u in all_usages)
    top_skills = skill_counts.most_common(20)

    # Skills by day
    skill_by_day: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for u in all_usages:
        day = u["timestamp"][:10]
        skill_by_day[day][u["skill_name"]] += 1

    # Average session length with/without skills
    sessions_with = []
    sessions_without = []
    for s in period_sessions:
        if session_skills.get(s.session_id):
            sessions_with.append(s.message_count)
        else:
            sessions_without.append(s.message_count)

    avg_with = sum(sessions_with) / len(sessions_with) if sessions_with else 0
    avg_without = sum(sessions_without) / len(sessions_without) if sessions_without else 0

    # Per-skill avg session length
    skill_session_lengths: Dict[str, List[int]] = defaultdict(list)
    for s in period_sessions:
        for skill in set(session_skills.get(s.session_id, [])):
            skill_session_lengths[skill].append(s.message_count)

    avg_session_length_with_skill = {
        skill: round(sum(lengths) / len(lengths), 1)
        for skill, lengths in skill_session_lengths.items()
    }

    # Skill chains — skills used in the same session
    chain_counts: Counter = Counter()
    for sid, skills in session_skills.items():
        unique = sorted(set(skills))
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                chain_counts[(unique[i], unique[j])] += 1

    skill_chains = [
        {"skill1": s1, "skill2": s2, "count": c}
        for (s1, s2), c in chain_counts.most_common(10)
        if c >= 2
    ]

    return {
        "skill_counts": dict(skill_counts),
        "top_skills": [{"name": n, "count": c} for n, c in top_skills],
        "skill_by_day": {d: dict(v) for d, v in skill_by_day.items()},
        "avg_session_length_with_skill": avg_session_length_with_skill,
        "avg_session_length_without_skill": round(avg_without, 1),
        "avg_session_length_with_any_skill": round(avg_with, 1),
        "skill_chains": skill_chains,
        "total_skill_invocations": len(all_usages),
        "sessions_using_skills": len(sessions_with),
        "sessions_without_skills": len(sessions_without),
        "unique_skills": len(skill_counts),
    }
