"""Daily dimensional observation extractor.

Runs each dimension's extraction on a day's sessions and stores observations.
Lightweight versions of the Wrapped extractors, designed for daily incremental use.
"""

import json
import re
from datetime import datetime, timedelta
from typing import List, Optional

from ..data.session_parser import SessionParser
from ..data.dimensions import save_observation, has_observations_for_date
from .llm import call_claude, parse_json_response


def extract_daily_observations(date: str, parser: SessionParser) -> dict:
    """Extract observations across all 5 dimensions for a given date."""
    sessions = _get_sessions_for_date(date, parser)
    if not sessions:
        return {"date": date, "observations_saved": 0, "skipped": "no sessions"}

    saved = 0
    results = {}

    for dimension, extractor in EXTRACTORS.items():
        if has_observations_for_date(dimension, date):
            continue
        try:
            obs = extractor(sessions, parser, date)
            if obs and obs.get("signal_count", 0) > 0:
                save_observation(
                    dimension=dimension,
                    date=date,
                    data=obs,
                    signal_count=obs.get("signal_count", 1),
                )
                saved += 1
                results[dimension] = obs
        except Exception as e:
            print(f"  Dimension {dimension} extraction failed for {date}: {e}")

    return {"date": date, "observations_saved": saved, "dimensions": results}


def _get_sessions_for_date(date: str, parser: SessionParser) -> list:
    """Get all sessions active on a given date."""
    target = datetime.strptime(date, "%Y-%m-%d").date()
    all_sessions = parser.get_all_sessions()
    return [s for s in all_sessions if s.start_time.date() == target]


# --- Individual Extractors ---


def _extract_energy(sessions: list, parser: SessionParser, date: str) -> dict:
    """Energy signals: time patterns, intensity, duration."""
    if not sessions:
        return {"signal_count": 0}

    hours = [s.start_time.hour for s in sessions]
    total_msgs = sum(s.message_count for s in sessions)
    total_mins = sum(
        max(1, (s.last_activity - s.start_time).total_seconds() / 60)
        for s in sessions
    )
    early = sum(1 for h in hours if h < 6)
    late = sum(1 for h in hours if h >= 22)

    return {
        "signal_count": len(sessions),
        "session_count": len(sessions),
        "total_messages": total_msgs,
        "total_minutes": round(total_mins),
        "hours_active": sorted(set(hours)),
        "early_sessions": early,
        "late_sessions": late,
        "peak_hour": max(set(hours), key=hours.count) if hours else None,
    }


def _extract_tempo(sessions: list, parser: SessionParser, date: str) -> dict:
    """Tempo signals: pace between messages, message lengths."""
    gaps = []
    msg_lengths = []

    for s in sessions[:10]:
        messages = parser.get_session_messages(s.session_id, limit=100)
        user_msgs = [m for m in messages if m.type == "user"]
        for m in user_msgs:
            msg_lengths.append(len(m.content))
        for i in range(1, len(user_msgs)):
            gap = (user_msgs[i].timestamp - user_msgs[i-1].timestamp).total_seconds()
            if 1 < gap < 3600:
                gaps.append(gap)

    if not msg_lengths:
        return {"signal_count": 0}

    return {
        "signal_count": len(msg_lengths),
        "avg_gap_seconds": round(sum(gaps) / len(gaps)) if gaps else 0,
        "avg_message_length": round(sum(msg_lengths) / len(msg_lengths)),
        "short_pct": round(100 * sum(1 for l in msg_lengths if l < 50) / len(msg_lengths)),
        "long_pct": round(100 * sum(1 for l in msg_lengths if l > 500) / len(msg_lengths)),
    }


def _extract_language(sessions: list, parser: SessionParser, date: str) -> dict:
    """Language signals: vocabulary, word frequency, phrasing."""
    all_text = []
    for s in sessions[:10]:
        messages = parser.get_session_messages(s.session_id, limit=100)
        for m in messages:
            if m.type == "user" and m.content:
                all_text.append(m.content)

    if not all_text:
        return {"signal_count": 0}

    full = " ".join(all_text).lower()
    words = re.findall(r'[a-z]{3,}', full)

    stop = {"the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
            "her", "was", "one", "our", "out", "has", "its", "his", "how", "man",
            "new", "now", "old", "see", "way", "who", "did", "get", "let", "say",
            "she", "too", "use", "this", "that", "with", "have", "from", "they",
            "been", "some", "what", "when", "will", "more", "just", "about", "into",
            "also", "than", "them", "each", "make", "like", "been", "most", "only"}

    freq = {}
    for w in words:
        if w not in stop:
            freq[w] = freq.get(w, 0) + 1

    top = sorted(freq.items(), key=lambda x: -x[1])[:10]

    return {
        "signal_count": len(all_text),
        "total_words": len(words),
        "unique_words": len(set(words)),
        "top_words": top,
        "messages_analyzed": len(all_text),
    }


TASTE_OBSERVATION_PROMPT = """Analyze these user messages for taste signals — revision requests, quality judgments, acceptance/rejection patterns.

Messages:
{messages}

Respond with JSON:
{{
  "revision_signals": ["specific revision requests found"],
  "quality_bar": "what 'good enough' seems to mean today",
  "signal_count": number_of_taste_signals_found
}}"""


def _extract_taste(sessions: list, parser: SessionParser, date: str) -> dict:
    """Taste signals: revisions, quality judgments, acceptance/rejection."""
    revision_keywords = [
        "no,", "not quite", "change", "make it", "too", "instead",
        "simpler", "shorter", "clearer", "that's not", "redo", "try again",
        "actually", "rewrite", "fix", "adjust",
    ]

    taste_msgs = []
    total_user = 0

    for s in sessions[:10]:
        messages = parser.get_session_messages(s.session_id, limit=100)
        for m in messages:
            if m.type == "user":
                total_user += 1
                if any(kw in m.content.lower() for kw in revision_keywords):
                    taste_msgs.append(m.content[:150])

    if not taste_msgs:
        return {"signal_count": 0, "revision_count": 0, "total_user_messages": total_user}

    # LLM analysis for qualitative signals
    llm_data = {}
    if len(taste_msgs) >= 3:
        prompt = TASTE_OBSERVATION_PROMPT.format(messages="\n---\n".join(taste_msgs[:10]))
        result = call_claude(prompt)
        if result:
            llm_data = parse_json_response(result) or {}

    return {
        "signal_count": len(taste_msgs),
        "revision_count": len(taste_msgs),
        "total_user_messages": total_user,
        "revision_ratio_pct": round(100 * len(taste_msgs) / total_user) if total_user else 0,
        **llm_data,
    }


REASONING_OBSERVATION_PROMPT = """Analyze these conversation openings and sequences for reasoning style signals.

First messages and sequences:
{sequences}

Respond with JSON:
{{
  "approach_today": "one sentence — how they approached problems today",
  "entry_point_style": "how they started (metaphor, question, command, data, etc.)",
  "signal_count": number_of_reasoning_signals
}}"""


def _extract_reasoning(sessions: list, parser: SessionParser, date: str) -> dict:
    """Reasoning signals: problem-solving approach, thinking style."""
    sequences = []

    for s in sessions[:10]:
        messages = parser.get_session_messages(s.session_id, limit=30)
        user_msgs = [m for m in messages if m.type == "user"]
        if user_msgs:
            seq = " → ".join(m.content[:80] for m in user_msgs[:3])
            sequences.append(seq)

    if not sequences:
        return {"signal_count": 0}

    # LLM analysis
    llm_data = {}
    if len(sequences) >= 2:
        prompt = REASONING_OBSERVATION_PROMPT.format(sequences="\n---\n".join(sequences[:8]))
        result = call_claude(prompt)
        if result:
            llm_data = parse_json_response(result) or {}

    first_lengths = []
    for s in sessions[:10]:
        messages = parser.get_session_messages(s.session_id, limit=5)
        user_msgs = [m for m in messages if m.type == "user"]
        if user_msgs:
            first_lengths.append(len(user_msgs[0].content))

    return {
        "signal_count": len(sequences),
        "sessions_analyzed": len(sequences),
        "avg_first_message_length": round(sum(first_lengths) / len(first_lengths)) if first_lengths else 0,
        **llm_data,
    }


EXTRACTORS = {
    "energy": _extract_energy,
    "tempo": _extract_tempo,
    "language": _extract_language,
    "taste": _extract_taste,
    "reasoning": _extract_reasoning,
}
