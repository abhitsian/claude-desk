"""Tool and agent usage analytics across sessions.

Extracts tool call data from conversation messages and computes
usage statistics, error rates, and agent spawn metrics.
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..data.models import ConversationMessage
from ..data.session_parser import SessionParser

ERROR_PATTERNS = re.compile(
    r"Error:|error:|Permission denied|ENOENT|EACCES|EPERM|"
    r"command not found|No such file|denied|rejected|failed|timed out",
    re.IGNORECASE,
)

AGENT_TOOL_NAMES = {"Agent", "AgentTool", "agent"}


def extract_tool_usage(session_id: str, parser: SessionParser) -> List[dict]:
    """Extract tool calls from a session's messages."""
    messages = parser.get_session_messages(session_id, limit=2000)
    usages: List[dict] = []

    # Build a map of tool_use_id -> tool info for correlating results
    pending_tools: Dict[str, dict] = {}

    for msg in messages:
        if msg.type == "assistant":
            # From tool_details (parsed structured data)
            for td in msg.tool_details:
                entry = {
                    "tool_name": td.name,
                    "timestamp": msg.timestamp.isoformat(),
                    "session_id": session_id,
                    "has_error": False,
                    "file_path": td.file_path,
                    "command": td.command,
                    "is_agent": td.name in AGENT_TOOL_NAMES,
                }
                usages.append(entry)

            # From raw tool_calls dicts
            for tc in msg.tool_calls:
                tool_name = tc.get("name") or tc.get("tool_name") or ""
                tool_id = tc.get("id") or tc.get("tool_use_id") or ""
                inp = tc.get("input") or tc.get("tool_input") or {}

                if tool_id:
                    pending_tools[tool_id] = {
                        "tool_name": tool_name,
                        "is_agent": tool_name in AGENT_TOOL_NAMES,
                    }

                # Only add if we didn't already capture via tool_details
                if not msg.tool_details:
                    entry = {
                        "tool_name": tool_name,
                        "timestamp": msg.timestamp.isoformat(),
                        "session_id": session_id,
                        "has_error": False,
                        "file_path": inp.get("file_path") or inp.get("path"),
                        "command": inp.get("command"),
                        "is_agent": tool_name in AGENT_TOOL_NAMES,
                    }
                    usages.append(entry)

        # Check tool results for errors
        if msg.type == "user" and msg.content:
            # Tool results come back as user messages
            if ERROR_PATTERNS.search(msg.content[:500]):
                # Mark the most recent tool call as errored
                if usages:
                    usages[-1]["has_error"] = True

    return usages


def _count_permission_denials(session_id: str, parser: SessionParser) -> int:
    """Count permission denial messages in a session."""
    messages = parser.get_session_messages(session_id, limit=2000)
    count = 0
    for msg in messages:
        if msg.content and re.search(
            r"denied|rejected|not allowed|permission.*denied|user denied",
            msg.content[:300],
            re.IGNORECASE,
        ):
            count += 1
    return count


def get_tool_stats(
    sessions: list,
    parser: SessionParser,
    days: int = 30,
) -> dict:
    """Compute tool usage statistics over a time period."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    period_sessions = [
        s for s in sessions
        if s.start_time.replace(tzinfo=timezone.utc) > cutoff
    ]

    all_usages: List[dict] = []
    total_denials = 0

    for s in period_sessions:
        try:
            usages = extract_tool_usage(s.session_id, parser)
            all_usages.extend(usages)
            total_denials += _count_permission_denials(s.session_id, parser)
        except Exception:
            continue

    # Tool counts and errors
    tool_counts: Counter = Counter()
    tool_errors: Counter = Counter()
    agent_types: Counter = Counter()

    for u in all_usages:
        name = u["tool_name"]
        tool_counts[name] += 1
        if u["has_error"]:
            tool_errors[name] += 1
        if u["is_agent"]:
            agent_types[name] += 1

    # Error rates (only for tools with 5+ calls)
    tool_error_rate = {}
    for name, count in tool_counts.items():
        if count >= 5:
            tool_error_rate[name] = round(tool_errors.get(name, 0) / count, 3)

    # Most error-prone (min 5 calls, sorted by error rate)
    most_error_prone = sorted(
        [
            {"name": n, "calls": tool_counts[n], "errors": tool_errors.get(n, 0), "rate": r}
            for n, r in tool_error_rate.items()
            if r > 0
        ],
        key=lambda x: -x["rate"],
    )[:5]

    # Tools by day
    tools_by_day: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for u in all_usages:
        day = u["timestamp"][:10]
        tools_by_day[day][u["tool_name"]] += 1

    top_tools = tool_counts.most_common(20)

    # Agent stats
    agent_usages = [u for u in all_usages if u["is_agent"]]

    return {
        "tool_counts": dict(tool_counts),
        "tool_errors": dict(tool_errors),
        "tool_error_rate": tool_error_rate,
        "top_tools": [{"name": n, "count": c} for n, c in top_tools],
        "most_error_prone": most_error_prone,
        "tools_by_day": {d: dict(v) for d, v in tools_by_day.items()},
        "permission_denials": total_denials,
        "total_tool_calls": len(all_usages),
        "unique_tools": len(tool_counts),
        "busiest_tools": [{"name": n, "count": c} for n, c in top_tools[:5]],
        "agent_spawns": len(agent_usages),
        "agent_types": dict(agent_types),
    }
