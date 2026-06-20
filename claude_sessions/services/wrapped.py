"""Wrapped — periodic dimensional analysis of Claude usage patterns.

Extracts 5 dimensions from session data:
- Taste: revision patterns, rejection language, quality signals
- Reasoning: problem-solving approach, thinking style
- Energy: when/where attention goes, productive hours, session intensity
- Tempo: pace of interaction, response cadence, time patterns
- Language: vocabulary fingerprint, word choice, phrasing patterns

Quantitative dimensions (energy, tempo, language) use pure data extraction.
Qualitative dimensions (taste, reasoning) use Claude for interpretation.
"""

import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..config import settings
from ..data.session_parser import SessionParser


# --- Energy Dimension (pure data) ---


def extract_energy(sessions: list, parser: SessionParser, days: int = 30) -> dict:
    """Analyze energy patterns — when, where, and how intensely you work."""
    cutoff = datetime.now() - timedelta(days=days)
    recent = [s for s in sessions if s.last_activity.replace(tzinfo=None) > cutoff]

    if not recent:
        return {}

    # Hour distribution
    hour_counts = Counter()
    hour_messages = defaultdict(int)
    for s in recent:
        hour = s.start_time.hour
        hour_counts[hour] += 1
        hour_messages[hour] += s.message_count

    peak_hour = max(hour_messages, key=hour_messages.get) if hour_messages else 0
    quiet_hours = [h for h in range(24) if hour_messages.get(h, 0) == 0]

    # Day of week
    dow_counts = Counter()
    dow_messages = defaultdict(int)
    for s in recent:
        dow = s.start_time.strftime("%A")
        dow_counts[dow] += 1
        dow_messages[dow] += s.message_count

    busiest_day = max(dow_messages, key=dow_messages.get) if dow_messages else "Unknown"

    # Session intensity (messages per minute)
    intensities = []
    for s in recent:
        duration = max(1, (s.last_activity - s.start_time).total_seconds() / 60)
        intensities.append({
            "session_id": s.session_id,
            "title": s.title or s.session_id[:8],
            "intensity": round(s.message_count / duration, 1),
            "messages": s.message_count,
            "duration_min": round(duration),
        })
    intensities.sort(key=lambda x: -x["intensity"])

    # Early bird / night owl
    early_sessions = [s for s in recent if s.start_time.hour < 6]
    late_sessions = [s for s in recent if s.start_time.hour >= 22]

    # Longest session
    sessions_by_duration = sorted(recent, key=lambda s: (s.last_activity - s.start_time).total_seconds(), reverse=True)
    longest = sessions_by_duration[0] if sessions_by_duration else None

    # Total stats
    total_messages = sum(s.message_count for s in recent)
    total_sessions = len(recent)
    total_hours = sum((s.last_activity - s.start_time).total_seconds() for s in recent) / 3600

    return {
        "total_sessions": total_sessions,
        "total_messages": total_messages,
        "total_hours": round(total_hours, 1),
        "peak_hour": peak_hour,
        "peak_hour_label": f"{peak_hour}:00–{peak_hour+1}:00",
        "busiest_day": busiest_day,
        "busiest_day_sessions": dow_counts.get(busiest_day, 0),
        "early_bird_count": len(early_sessions),
        "night_owl_count": len(late_sessions),
        "most_intense": intensities[:3],
        "longest_session": {
            "title": longest.title or longest.session_id[:8],
            "duration_min": round((longest.last_activity - longest.start_time).total_seconds() / 60),
            "messages": longest.message_count,
        } if longest else None,
        "hour_distribution": dict(sorted(hour_messages.items())),
        "day_distribution": dict(dow_messages),
    }


# --- Tempo Dimension (pure data) ---


def extract_tempo(sessions: list, parser: SessionParser, days: int = 30) -> dict:
    """Analyze tempo — pace, rhythm, response cadence."""
    cutoff = datetime.now() - timedelta(days=days)
    recent = [s for s in sessions if s.last_activity.replace(tzinfo=None) > cutoff]

    if not recent:
        return {}

    # Sample messages for timing analysis (cap to avoid processing thousands)
    all_gaps = []
    session_paces = []

    for s in recent[:30]:
        messages = parser.get_session_messages(s.session_id, limit=200)
        user_msgs = [m for m in messages if m.type == "user"]

        if len(user_msgs) < 2:
            continue

        # Gaps between user messages
        gaps = []
        for i in range(1, len(user_msgs)):
            gap = (user_msgs[i].timestamp - user_msgs[i-1].timestamp).total_seconds()
            if 1 < gap < 3600:  # Ignore <1s (automated) and >1hr (break)
                gaps.append(gap)

        if gaps:
            avg_gap = sum(gaps) / len(gaps)
            all_gaps.extend(gaps)
            session_paces.append({
                "session_id": s.session_id,
                "title": s.title or s.session_id[:8],
                "avg_gap_seconds": round(avg_gap),
                "message_count": len(user_msgs),
            })

    # Message length distribution
    msg_lengths = []
    for s in recent[:20]:
        messages = parser.get_session_messages(s.session_id, limit=100)
        for m in messages:
            if m.type == "user":
                msg_lengths.append(len(m.content))

    avg_message_length = round(sum(msg_lengths) / len(msg_lengths)) if msg_lengths else 0
    short_msgs = sum(1 for l in msg_lengths if l < 50)
    long_msgs = sum(1 for l in msg_lengths if l > 500)

    # Streak detection — consecutive days with sessions
    dates_active = sorted(set(s.start_time.strftime("%Y-%m-%d") for s in recent))
    max_streak = 0
    current_streak = 1
    for i in range(1, len(dates_active)):
        d1 = datetime.strptime(dates_active[i-1], "%Y-%m-%d")
        d2 = datetime.strptime(dates_active[i], "%Y-%m-%d")
        if (d2 - d1).days == 1:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 1
    max_streak = max(max_streak, current_streak)

    median_gap = sorted(all_gaps)[len(all_gaps)//2] if all_gaps else 0

    return {
        "avg_gap_seconds": round(sum(all_gaps) / len(all_gaps)) if all_gaps else 0,
        "median_gap_seconds": round(median_gap),
        "fastest_pace": min(session_paces, key=lambda x: x["avg_gap_seconds"]) if session_paces else None,
        "slowest_pace": max(session_paces, key=lambda x: x["avg_gap_seconds"]) if session_paces else None,
        "avg_message_length": avg_message_length,
        "short_messages_pct": round(100 * short_msgs / len(msg_lengths)) if msg_lengths else 0,
        "long_messages_pct": round(100 * long_msgs / len(msg_lengths)) if msg_lengths else 0,
        "total_messages_sampled": len(msg_lengths),
        "max_streak_days": max_streak,
        "days_active": len(dates_active),
        "days_in_period": days,
    }


# --- Language Dimension (pure data) ---


def extract_language(sessions: list, parser: SessionParser, days: int = 30) -> dict:
    """Analyze language patterns — vocabulary, word choice, phrasing."""
    cutoff = datetime.now() - timedelta(days=days)
    recent = [s for s in sessions if s.last_activity.replace(tzinfo=None) > cutoff]

    if not recent:
        return {}

    # Collect all user text
    all_text = []
    for s in recent[:30]:
        messages = parser.get_session_messages(s.session_id, limit=100)
        for m in messages:
            if m.type == "user" and m.content:
                all_text.append(m.content)

    full_text = " ".join(all_text).lower()
    words = re.findall(r'[a-z]+', full_text)

    if not words:
        return {}

    # Word frequency (excluding common stop words)
    stop_words = {
        "the", "a", "an", "is", "it", "to", "in", "for", "of", "and", "or",
        "but", "not", "with", "this", "that", "on", "at", "by", "from", "as",
        "be", "was", "are", "have", "has", "had", "do", "does", "did", "will",
        "can", "could", "would", "should", "may", "might", "shall", "i", "you",
        "he", "she", "we", "they", "me", "my", "your", "his", "her", "its",
        "our", "their", "what", "which", "who", "when", "where", "how", "why",
        "all", "each", "every", "both", "few", "more", "most", "some", "any",
        "no", "just", "about", "up", "out", "so", "if", "then", "than",
        "too", "very", "also", "here", "there", "now", "only", "even", "back",
        "get", "got", "make", "like", "know", "think", "see", "want", "use",
        "need", "one", "two", "new", "way", "first", "time", "been", "into",
    }

    word_freq = Counter(w for w in words if w not in stop_words and len(w) > 2)
    top_words = word_freq.most_common(20)

    # Unique vocabulary size
    unique_words = len(set(words))
    total_words = len(words)
    vocab_richness = round(unique_words / total_words * 100, 1) if total_words else 0

    # Signature phrases (2-grams)
    bigrams = Counter()
    for i in range(len(words) - 1):
        if words[i] not in stop_words or words[i+1] not in stop_words:
            bg = f"{words[i]} {words[i+1]}"
            bigrams[bg] += 1
    top_phrases = [(p, c) for p, c in bigrams.most_common(30) if c >= 3][:10]

    # Words never used (common corporate/AI words)
    corporate_words = ["synergy", "leverage", "bandwidth", "paradigm", "disrupt",
                       "ecosystem", "holistic", "scalable", "empower", "optimize",
                       "facilitate", "streamline", "incentivize", "actionable"]
    never_used = [w for w in corporate_words if w not in full_text]

    # Question ratio
    questions = sum(1 for t in all_text if "?" in t)
    question_ratio = round(100 * questions / len(all_text)) if all_text else 0

    # Command style detection
    imperative = sum(1 for t in all_text if t.strip().split()[0].lower() in
                     {"make", "create", "build", "add", "fix", "update", "change",
                      "write", "show", "run", "check", "find", "help", "open",
                      "remove", "delete", "move", "set", "turn", "give"}) if all_text else 0
    imperative_ratio = round(100 * imperative / len(all_text)) if all_text else 0

    return {
        "total_words": total_words,
        "unique_words": unique_words,
        "vocab_richness_pct": vocab_richness,
        "top_words": top_words,
        "top_phrases": top_phrases,
        "never_used_corporate": never_used[:8],
        "question_ratio_pct": question_ratio,
        "imperative_ratio_pct": imperative_ratio,
        "messages_analyzed": len(all_text),
    }


# --- Taste Dimension (LLM-powered) ---


TASTE_PROMPT = """You are analyzing someone's taste — their quality judgments, revision patterns, and aesthetic preferences — based on their conversations with Claude.

Here are samples of their revision requests, rejections, and quality-related messages:

{samples}

Analyze their taste across these dimensions and respond with JSON:
{{
  "revision_pattern": "one sentence describing their most common type of revision (shorter, clearer, more specific, different tone, etc.)",
  "quality_bar": "one sentence describing what 'good enough' means to them",
  "aesthetic": "one sentence describing their design/presentation preferences",
  "pet_peeves": ["things they consistently reject or correct"],
  "signature_moves": ["quality choices that define their style"],
  "taste_in_one_line": "a single sentence that captures their taste"
}}"""


def extract_taste(sessions: list, parser: SessionParser, days: int = 30) -> dict:
    """Analyze taste patterns — revision requests, rejections, quality signals."""
    cutoff = datetime.now() - timedelta(days=days)
    recent = [s for s in sessions if s.last_activity.replace(tzinfo=None) > cutoff]

    if not recent:
        return {}

    # Find revision/rejection signals in user messages
    revision_keywords = [
        "no,", "not quite", "change", "make it", "too", "instead",
        "more", "less", "simpler", "shorter", "longer", "clearer",
        "that's not", "wrong", "redo", "try again", "actually",
        "rewrite", "rephrase", "fix", "adjust",
    ]

    taste_samples = []
    revision_count = 0
    total_user_msgs = 0

    for s in recent[:25]:
        messages = parser.get_session_messages(s.session_id, limit=200)
        for m in messages:
            if m.type == "user":
                total_user_msgs += 1
                content_lower = m.content.lower()
                if any(kw in content_lower for kw in revision_keywords):
                    revision_count += 1
                    taste_samples.append(m.content[:200])

    revision_ratio = round(100 * revision_count / total_user_msgs) if total_user_msgs else 0

    # Get LLM analysis if we have enough samples
    llm_analysis = {}
    if len(taste_samples) >= 5:
        samples_text = "\n---\n".join(taste_samples[:20])
        prompt = TASTE_PROMPT.format(samples=samples_text)
        result = _call_claude_wrapped(prompt)
        if result:
            try:
                llm_analysis = json.loads(result)
            except json.JSONDecodeError:
                match = re.search(r'\{[\s\S]*\}', result)
                if match:
                    try:
                        llm_analysis = json.loads(match.group())
                    except json.JSONDecodeError:
                        pass

    return {
        "revision_count": revision_count,
        "revision_ratio_pct": revision_ratio,
        "total_messages": total_user_msgs,
        "samples_found": len(taste_samples),
        **llm_analysis,
    }


# --- Reasoning Dimension (LLM-powered) ---


REASONING_PROMPT = """You are analyzing how someone thinks — their reasoning style, problem-solving approach, and thinking patterns — based on their conversations with Claude.

Here are samples of how they approach problems, ask questions, and work through ideas:

{samples}

Analyze their reasoning style and respond with JSON:
{{
  "primary_style": "one of: analogical (thinks in metaphors), analytical (breaks into parts), intuitive (follows feeling first), systematic (follows process), dialectical (argues both sides)",
  "entry_point": "how they typically start a problem — what their first message looks like",
  "thinking_sequence": "the typical order of their thinking (e.g., metaphor → test → build, or question → data → conclusion)",
  "strength": "what they're best at in their reasoning",
  "gap": "what they consistently skip or underweight",
  "reasoning_in_one_line": "a single sentence that captures how they think"
}}"""


def extract_reasoning(sessions: list, parser: SessionParser, days: int = 30) -> dict:
    """Analyze reasoning patterns — how they think through problems."""
    cutoff = datetime.now() - timedelta(days=days)
    recent = [s for s in sessions if s.last_activity.replace(tzinfo=None) > cutoff]

    if not recent:
        return {}

    # Collect first messages and problem-solving sequences
    first_messages = []
    reasoning_samples = []

    for s in recent[:25]:
        messages = parser.get_session_messages(s.session_id, limit=50)
        user_msgs = [m for m in messages if m.type == "user"]
        if user_msgs:
            first_messages.append(user_msgs[0].content[:200])
            # Get first 3 user messages as a reasoning sequence
            sequence = " → ".join(m.content[:100] for m in user_msgs[:3])
            reasoning_samples.append(sequence)

    # Get LLM analysis
    llm_analysis = {}
    if len(reasoning_samples) >= 5:
        samples_text = "\n---\n".join(reasoning_samples[:15])
        prompt = REASONING_PROMPT.format(samples=samples_text)
        result = _call_claude_wrapped(prompt)
        if result:
            try:
                llm_analysis = json.loads(result)
            except json.JSONDecodeError:
                match = re.search(r'\{[\s\S]*\}', result)
                if match:
                    try:
                        llm_analysis = json.loads(match.group())
                    except json.JSONDecodeError:
                        pass

    # Quantitative: first message length distribution
    first_msg_lengths = [len(m) for m in first_messages]
    avg_first_msg = round(sum(first_msg_lengths) / len(first_msg_lengths)) if first_msg_lengths else 0

    return {
        "sessions_analyzed": len(recent[:25]),
        "avg_first_message_length": avg_first_msg,
        **llm_analysis,
    }


# --- Wrapped Generator ---


def generate_wrapped(parser: SessionParser, days: int = 30) -> dict:
    """Generate a full Wrapped report across all dimensions."""
    sessions = parser.get_all_sessions()

    # Also include archived sessions
    try:
        from ..data.archive import SessionArchive
        from ..data.models import SessionMetadata
        archive = SessionArchive()
        archived = archive.get_archived_only(limit=500)
        seen = {s.session_id for s in sessions}
        for a in archived:
            if a["session_id"] not in seen:
                sessions.append(SessionMetadata(
                    session_id=a["session_id"],
                    project_path=a.get("project_path") or "",
                    start_time=datetime.fromisoformat(a["start_time"]),
                    last_activity=datetime.fromisoformat(a["last_activity"]),
                    message_count=a["message_count"],
                    title=a.get("title"),
                    model_used=a.get("model_used"),
                    total_input_tokens=a.get("total_input_tokens", 0),
                    total_output_tokens=a.get("total_output_tokens", 0),
                ))
    except Exception:
        pass

    print("Generating Wrapped...")
    print("  Extracting energy...", flush=True)
    energy = extract_energy(sessions, parser, days)

    print("  Extracting tempo...", flush=True)
    tempo = extract_tempo(sessions, parser, days)

    print("  Extracting language...", flush=True)
    language = extract_language(sessions, parser, days)

    print("  Extracting taste (LLM)...", flush=True)
    taste = extract_taste(sessions, parser, days)

    print("  Extracting reasoning (LLM)...", flush=True)
    reasoning = extract_reasoning(sessions, parser, days)

    period_end = datetime.now().strftime("%Y-%m-%d")
    period_start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Generate the narrative layer — this is what makes it shareable
    print("  Writing narrative (LLM)...", flush=True)
    narrative = generate_narrative({
        "energy": energy,
        "tempo": tempo,
        "language": language,
        "taste": taste,
        "reasoning": reasoning,
        "period_start": period_start,
        "period_end": period_end,
        "days": days,
    })

    return {
        "period_start": period_start,
        "period_end": period_end,
        "days": days,
        "energy": energy,
        "tempo": tempo,
        "language": language,
        "taste": taste,
        "reasoning": reasoning,
        "narrative": narrative,
    }


# --- Narrative Generator (the viral layer) ---


NARRATIVE_PROMPT = """You are writing the copy for someone's "Claude Wrapped" — a Spotify-Wrapped-style report about how they use Claude. Your job is to make this so specific, funny, and surprisingly true that they screenshot it and share it.

Here is their raw data from the past {days} days:

{raw_data}

Write the narrative layer. Respond with JSON:

{{
  "archetype": "A 2-3 word personality label, like Spotify's listening personalities. Examples: 'The Midnight Architect', 'The Revision Addict', 'The Metaphor Engine', 'The Pruning Monk'. It should feel like a character class in a game — specific, aspirational, slightly funny.",

  "archetype_description": "One sentence explaining the archetype. Written in second person. Should make them laugh because it's true.",

  "hero_stat_commentary": "A funny one-liner about their total sessions/messages number. Reference something real for scale — e.g., 'That's more words than The Great Gatsby. You wrote The Great Gatsby to an AI.' or 'You've had more conversations with Claude than most people have with their therapist.'",

  "roasts": [
    "A gentle, specific roast based on their actual data. Not mean — funny because it's precisely true. e.g., 'You say you want shorter responses. Your average prompt is 847 characters. The irony is not lost on Claude.'",
    "Another roast. Target a contradiction or funny pattern.",
    "A third roast. The best roasts reference specific behaviors from the data."
  ],

  "counter_intuitive": [
    "A surprising insight that contradicts what they'd expect. e.g., 'Your most productive sessions aren't the long ones — they're the 8-minute bursts at 2am when you already know what you want.'",
    "Another counter-intuitive finding from the data."
  ],

  "energy_headline": "A punchy, fun headline for their energy card. Not 'Your peak hour is 9am' — more like 'You and Claude have a standing 9am meeting. Neither of you scheduled it.'",

  "tempo_headline": "A headline for tempo. e.g., '34 seconds between messages. You type like you're defusing a bomb.'",

  "language_headline": "A headline for language. Reference their actual top words or vocabulary quirks. e.g., 'You said \"crisp\" 23 times. Claude now has trust issues with the word \"crisp.\"'",

  "taste_headline": "A headline for taste. e.g., 'You rejected 47 first drafts. Michelangelo had similar numbers with marble.'",

  "reasoning_headline": "A headline for reasoning. e.g., 'You think in metaphors. Every problem is a movie scene before it's a solution.'",

  "closing_line": "The final one-liner that captures everything. This is the screenshot moment. It should be so precisely them that they feel seen. One sentence.",

  "share_card_lines": [
    "Line 1 for a compact share card (the screenshot people post). Max 60 chars.",
    "Line 2. A surprising stat.",
    "Line 3. The roast.",
    "Line 4. The closer."
  ]
}}

Rules:
- Be specific. Reference actual numbers, words, and patterns from their data. Generic humor isn't funny.
- The best jokes are observations, not punchlines. "You edited the word 'improved' to 'strengthened' to 'elevated' and back to 'improved'" is funnier than any joke you could write.
- Don't be mean. Be warm. This is someone's AI showing it paid attention.
- Brevity. Every line should be tweetable. No filler.
- Don't use therapy language, don't be sycophantic, don't use exclamation marks.
- The archetype should feel like a compliment wrapped in a joke."""


def generate_narrative(raw_data: dict) -> dict:
    """Generate the shareable narrative layer from raw Wrapped data."""
    data_text = json.dumps(raw_data, indent=2, default=str)
    prompt = NARRATIVE_PROMPT.format(days=raw_data.get("days", 30), raw_data=data_text)

    result = _call_claude_wrapped(prompt)
    if not result:
        return {}

    try:
        parsed = json.loads(result)
        return parsed
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', result)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {}


# --- Claude CLI helper ---


def _call_claude_wrapped(prompt: str) -> Optional[str]:
    """Call Claude for qualitative analysis."""
    try:
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--system-prompt", "You are analyzing conversation patterns. Respond with valid JSON only. No markdown fences.",
                "--output-format", "json",
                "--model", "sonnet",
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return None
        try:
            cli = json.loads(result.stdout)
            if isinstance(cli, dict):
                return cli.get("result", result.stdout)
            return result.stdout
        except json.JSONDecodeError:
            return result.stdout
    except Exception as e:
        print(f"  Wrapped LLM call failed: {e}")
        return None
