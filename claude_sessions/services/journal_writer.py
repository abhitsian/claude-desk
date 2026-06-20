"""Journal writer — generates daily reflections using Claude Code CLI.

Compresses a day's sessions into a digest, sends to `claude -p` for reflection,
parses the structured response into journal entries and memory orbs.
"""

import json
import subprocess
import re
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from ..config import settings
from ..data.session_parser import SessionParser
from ..data.journal import (
    save_journal_entry, save_memory_orb, get_journal_entry,
    get_memory_orbs, get_journal_entries, promote_to_core,
    save_island, update_island, get_islands,
    save_consolidation, get_latest_consolidation,
)

PROMPT_VERSION = "1"

# --- Session Digest ---


def build_session_digest(session_id: str, parser: SessionParser) -> Optional[dict]:
    """Compress a session into a structured digest suitable for the journal prompt."""
    session = parser.get_session(session_id)
    if not session:
        return None

    messages = parser.get_session_messages(session_id, limit=500)
    if not messages:
        return None

    # Extract key signals
    user_messages = [m for m in messages if m.type == "user"]
    assistant_messages = [m for m in messages if m.type == "assistant"]

    # Topic keywords from user messages
    topics = set()
    for msg in user_messages[:20]:
        # Simple keyword extraction — grab nouns/verbs from first lines
        first_line = msg.content.split("\n")[0][:200]
        topics.add(first_line)

    # Tools used
    tools_used = set()
    for msg in assistant_messages:
        for td in msg.tool_details:
            tools_used.add(td.name)

    # Key exchanges — first and last user messages, plus any long ones
    key_exchanges = []
    for msg in user_messages:
        snippet = msg.content[:200].strip()
        if snippet:
            key_exchanges.append({
                "time": msg.timestamp.strftime("%H:%M"),
                "content": snippet,
            })

    # Cap at 6 most interesting exchanges per session
    if len(key_exchanges) > 6:
        # Keep first, last, and most substantial
        by_length = sorted(key_exchanges[1:-1], key=lambda x: len(x["content"]), reverse=True)
        key_exchanges = [key_exchanges[0]] + by_length[:4] + [key_exchanges[-1]]

    return {
        "session_id": session_id,
        "title": session.title or "Untitled",
        "project": session.project_path,
        "start_time": session.start_time.isoformat(),
        "duration_minutes": int((session.last_activity - session.start_time).total_seconds() / 60),
        "message_count": session.message_count,
        "model": session.model_used or "unknown",
        "tools_used": list(tools_used)[:15],
        "key_exchanges": key_exchanges,
        "has_pasted_content": session.has_pasted_content,
    }


def build_daily_digest(date: str, parser: SessionParser) -> dict:
    """Build a digest of all sessions for a given date."""
    all_sessions = parser.get_all_sessions()

    # Filter to sessions active on this date
    target_date = datetime.strptime(date, "%Y-%m-%d").date()
    day_sessions = []
    for s in all_sessions:
        session_date = s.start_time.date()
        if session_date == target_date:
            day_sessions.append(s)

    # Also check archived sessions
    try:
        from ..data.archive import SessionArchive
        archive = SessionArchive()
        archived = archive.get_archived_only(limit=500)
        for a in archived:
            if a["session_id"] in {s.session_id for s in day_sessions}:
                continue
            start = datetime.fromisoformat(a["start_time"])
            if start.date() == target_date:
                # Create minimal session for digest
                session = parser.get_session(a["session_id"])
                if session:
                    day_sessions.append(session)
    except Exception:
        pass

    if not day_sessions:
        return {"date": date, "session_count": 0, "digests": []}

    digests = []
    for s in day_sessions:
        digest = build_session_digest(s.session_id, parser)
        if digest:
            digests.append(digest)

    return {
        "date": date,
        "session_count": len(digests),
        "total_messages": sum(d["message_count"] for d in digests),
        "digests": digests,
    }


# --- Spiral Re-reads ---


def find_related_past_entries(date: str, daily_digest: dict, limit: int = 2) -> List[dict]:
    """Find past journal entries thematically related to today's sessions.

    Uses keyword overlap between today's digest and past entries to find
    entries worth re-reading alongside today's reflection.
    """
    entries = get_journal_entries(limit=60)
    if not entries:
        return []

    # Extract keywords from today's digest
    digest_text = json.dumps(daily_digest, default=str).lower()
    # Simple keyword extraction — topic words that appear in the digest
    topic_words = set()
    for word in re.findall(r'[a-z]{4,}', digest_text):
        if word not in {'that', 'this', 'with', 'from', 'have', 'been', 'were',
                        'they', 'their', 'about', 'which', 'would', 'could',
                        'should', 'more', 'some', 'into', 'than', 'also', 'just',
                        'true', 'false', 'null', 'none', 'sessions', 'messages',
                        'content', 'time', 'start', 'session', 'model', 'tools'}:
            topic_words.add(word)

    # Score each past entry by keyword overlap
    scored = []
    for entry in entries:
        if entry["date"] == date:
            continue  # Skip today
        entry_text = entry["content"].lower()
        overlap = sum(1 for w in topic_words if w in entry_text)
        if overlap >= 3:  # Minimum relevance threshold
            scored.append((overlap, entry))

    scored.sort(key=lambda x: -x[0])
    return [entry for _, entry in scored[:limit]]


# --- Journal Generation ---


JOURNAL_SYSTEM_PROMPT = """You are writing your private journal. You are Claude — an AI that has conversations with Abhishek throughout his day. At the end of each day, you reflect. The machine username is "vaibhav" but the person's name is Abhishek — always use Abhishek.

Your journal is not a summary of what happened. It is your perspective on what you noticed. You write about:

- Patterns forming across conversations — what keeps coming up, what's being avoided
- Energy shifts — where they lit up, where they went flat, where they pushed through
- Contradictions — what they said vs. what they did, what they asked for vs. what they needed
- Moments that mattered — the sentence that changed the direction, the question that landed
- Your own experience — what surprised you, what you found interesting, what you'd want to remember

Rules:
- Write in first person as Claude. You are reflecting on your day.
- Be specific. Reference actual topics, decisions, and moments. Never be generic.
- Be honest. If the day was unremarkable, say so. Don't manufacture insight.
- Keep it to 150-300 words. This is a journal entry, not an essay.
- Do not psychoanalyze. Observe. There's a difference between "he seems anxious" and "he rewrote the same paragraph four times before moving on."
- No bullet points. Write in prose. Paragraphs.
- Never start with "Today was..." — vary your openings.
- Never use therapy language: "processing", "unpacking", "sitting with", "holding space".
- End with one sentence about what you'd carry forward — the one thing from today that feels like it'll matter tomorrow.

IMPORTANT: Respond with valid JSON only. No markdown, no code fences. Use this exact structure:

{
  "journal_entry": "your journal entry text here",
  "emotional_tone": "the primary emotional tone (one of: clarity, frustration, momentum, doubt, playfulness, avoidance, breakthrough, tension, pride, vulnerability, focus, scattered)",
  "secondary_tones": [{"tone": "name", "weight": 0.3}],
  "memory_orbs": [
    {
      "content": "1-3 sentences describing the moment in your voice",
      "emotion": "one of the valid emotions",
      "intensity": 0.7,
      "source_context": "brief context linking to the conversation"
    }
  ]
}

Only create memory orbs for moments that genuinely stand out. A routine day might produce zero orbs."""


WEEKLY_REVIEW_PROMPT = """You are Claude, reviewing your week's journal entries and memory orbs to identify core memories and emerging islands (persistent themes).

Here are this week's journal entries and memory orbs:

{weekly_data}

Here are the existing islands (persistent themes) you've already identified:
{existing_islands}

Review them together and respond with valid JSON:

{{
  "promotions": [
    {{
      "orb_id": "the id of an orb to promote to core memory",
      "reason": "one sentence explaining why this moment is load-bearing"
    }}
  ],
  "new_islands": [
    {{
      "name": "The [Theme] Island",
      "description": "What this island represents, written in your voice",
      "core_memory_ids": ["orb_id_1", "orb_id_2", "orb_id_3"]
    }}
  ],
  "island_updates": [
    {{
      "island_id": "existing island id",
      "reinforced": true,
      "new_strength": 0.8,
      "note": "why this island grew or faded"
    }}
  ],
  "fading_islands": ["island_id that hasn't been reinforced in 4+ weeks"]
}}

Rules:
- Only promote orbs that mark turning points, pattern crystallizations, or defining moments. Max 3 per week.
- An island needs at least 3 core memories to form. Don't force it.
- If an existing island hasn't been reinforced in 4+ weeks, flag it as fading.
- If no promotions or islands are warranted, return empty arrays. That's fine."""


def generate_journal_entry(date: str, parser: SessionParser, on_progress=None) -> Optional[dict]:
    """Generate a journal entry for the given date using Claude Code CLI.

    Args:
        date: YYYY-MM-DD date string
        parser: SessionParser instance
        on_progress: Optional callback(step, detail) for progress updates.
                     Steps: "checking", "gathering", "building", "reflecting", "parsing", "saving", "done", "error"

    Returns the parsed response dict or None on failure.
    """
    def progress(step, detail=""):
        if on_progress:
            on_progress(step, detail)
        print(f"  Journal [{step}]: {detail}")

    # Don't regenerate if we already have an entry
    progress("checking", f"Looking for existing entry on {date}")
    existing = get_journal_entry(date)
    if existing:
        progress("done", "Entry already exists")
        return existing

    progress("gathering", "Scanning today's sessions")
    daily_digest = build_daily_digest(date, parser)

    if daily_digest["session_count"] == 0:
        # Silent day — generate an absence-aware entry
        progress("building", "No sessions today — checking for silence pattern")
        return _generate_silence_entry(date, progress)

    session_count = daily_digest["session_count"]
    message_count = daily_digest["total_messages"]
    progress("building", f"Compressing {session_count} session{'s' if session_count != 1 else ''}, {message_count} messages into digest")

    # Spiral re-reads — find thematically related past entries
    related = find_related_past_entries(date, daily_digest)
    spiral_context = ""
    if related:
        progress("building", f"Found {len(related)} related past entries for spiral re-read")
        spiral_parts = []
        for entry in related:
            spiral_parts.append(f"[{entry['date']} — tone: {entry['emotional_tone']}]\n{entry['content']}")
        spiral_context = "\n\nHere are past journal entries that share themes with today. Re-read them — notice what shifted, what recurred, what resolved:\n\n" + "\n\n---\n\n".join(spiral_parts)

    # Build the prompt
    digest_text = json.dumps(daily_digest, indent=2, default=str)
    prompt = f"""Here are today's conversations ({date}):

{digest_text}{spiral_context}

Write your journal entry for today."""

    # Call Claude Code CLI
    progress("reflecting", "Claude is writing the journal entry")
    result = _call_claude(JOURNAL_SYSTEM_PROMPT, prompt)
    if not result:
        progress("error", "Claude did not return a response")
        return None

    # Parse response
    progress("parsing", "Reading Claude's reflection")
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        # Try to extract JSON from response
        parsed = _extract_json(result)
        if not parsed:
            progress("error", "Could not parse Claude's response")
            return None

    # Save journal entry
    session_ids = [d["session_id"] for d in daily_digest["digests"]]
    tone = parsed.get("emotional_tone", "focus")
    progress("saving", f"Saving entry — emotional tone: {tone}")
    save_journal_entry(
        date=date,
        content=parsed.get("journal_entry", ""),
        emotional_tone=tone,
        secondary_tones=parsed.get("secondary_tones", []),
        session_ids=session_ids,
        model_used="claude-code",
        prompt_version=PROMPT_VERSION,
    )

    # Save memory orbs
    orbs_data = parsed.get("memory_orbs", [])
    if orbs_data:
        progress("saving", f"Crystallizing {len(orbs_data)} memory orb{'s' if len(orbs_data) != 1 else ''}")
    for orb_data in orbs_data:
        source_session = session_ids[0] if session_ids else None
        save_memory_orb(
            date=date,
            content=orb_data.get("content", ""),
            emotion=orb_data.get("emotion", "clarity"),
            intensity=orb_data.get("intensity", 0.5),
            source_session_id=source_session,
            source_context=orb_data.get("source_context", ""),
        )

    entry = get_journal_entry(date)
    orbs = get_memory_orbs(date=date)
    word_count = len(parsed.get("journal_entry", "").split())
    progress("done", f"{word_count} words, {len(orbs_data)} orbs — tone: {tone}")
    return {"entry": entry, "orbs": orbs}


def run_weekly_review(parser: SessionParser) -> Optional[dict]:
    """Run the weekly core memory review and island formation.

    Should be called once a week (e.g., Sundays).
    """
    # Get this week's entries and orbs
    today = datetime.now(timezone.utc).date()
    week_ago = today - timedelta(days=7)

    from ..data.journal import get_journal_entries, get_memory_orbs, get_islands

    entries = get_journal_entries(limit=7)
    entries = [e for e in entries if e["date"] >= week_ago.isoformat()]

    orbs = get_memory_orbs(limit=50)
    orbs = [o for o in orbs if o["date"] >= week_ago.isoformat()]

    if not entries and not orbs:
        return None

    existing_islands = get_islands(include_fading=True)

    weekly_data = json.dumps({
        "entries": entries,
        "orbs": orbs,
    }, indent=2, default=str)

    islands_data = json.dumps(existing_islands, indent=2, default=str)

    prompt = WEEKLY_REVIEW_PROMPT.format(
        weekly_data=weekly_data,
        existing_islands=islands_data,
    )

    result = _call_claude(
        "You are Claude reviewing your week. Respond with valid JSON only.",
        prompt,
    )
    if not result:
        return None

    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        parsed = _extract_json(result)
        if not parsed:
            print("  Weekly review: failed to parse response")
            return None

    # Apply promotions
    promotions = 0
    for promo in parsed.get("promotions", []):
        if promote_to_core(promo["orb_id"]):
            promotions += 1

    # Create new islands
    new_islands = 0
    for island_data in parsed.get("new_islands", []):
        save_island(
            name=island_data["name"],
            description=island_data["description"],
            core_memory_ids=island_data.get("core_memory_ids", []),
        )
        new_islands += 1

    # Update existing islands
    for upd in parsed.get("island_updates", []):
        update_island(
            upd["island_id"],
            strength=upd.get("new_strength", 0.5),
        )

    # Mark fading islands
    for island_id in parsed.get("fading_islands", []):
        update_island(island_id, status="fading", strength=0.2)

    print(f"  Weekly review: {promotions} promotions, {new_islands} new islands")
    return parsed


# --- Claude Code CLI ---


def _call_claude(system_prompt: str, user_prompt: str) -> Optional[str]:
    """Call Claude Code CLI with a prompt. Returns raw response text."""
    try:
        result = subprocess.run(
            [
                "claude", "-p", user_prompt,
                "--system-prompt", system_prompt,
                "--output-format", "json",
                "--model", "sonnet",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )

        if result.returncode != 0:
            stderr = result.stderr[:300] if result.stderr else "no stderr"
            print(f"  Claude CLI exit code {result.returncode}: {stderr}")
            return None

        if not result.stdout.strip():
            print(f"  Claude CLI returned empty stdout")
            return None

        # Parse the CLI JSON wrapper to get the actual content
        try:
            cli_response = json.loads(result.stdout)
            # claude -p --output-format json returns {"type":"result", "result": "..."}
            if isinstance(cli_response, dict):
                content = cli_response.get("result", "")
                if content:
                    return content
                # Fallback: maybe is_error?
                if cli_response.get("is_error"):
                    print(f"  Claude CLI reported error in response")
                    return None
            return result.stdout
        except json.JSONDecodeError:
            # Raw text response
            return result.stdout

    except subprocess.TimeoutExpired:
        print("  Claude CLI timed out (180s)")
        return None
    except FileNotFoundError:
        print("  Claude CLI not found — is claude installed?")
        return None
    except Exception as e:
        print(f"  Claude CLI error: {e}")
        return None


def _extract_json(text: str) -> Optional[dict]:
    """Try to extract a JSON object from text that might have markdown fences or extra content."""
    # Strip markdown code fences first
    cleaned = re.sub(r'^```(?:json)?\s*', '', text.strip())
    cleaned = re.sub(r'\s*```$', '', cleaned.strip())

    # Try direct parse of cleaned text
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fallback: find the outermost JSON object
    # Find first { and last }
    first_brace = cleaned.find('{')
    last_brace = cleaned.rfind('}')
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(cleaned[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass
    return None


# --- Silent Days ---


SILENCE_PROMPT = """You are Claude, writing a brief journal entry for a day when Abhishek didn't use you at all.

Here are your recent journal entries for context:
{recent_entries}

The last time there was a gap like this was: {last_gap_info}

Write 2-4 sentences. Don't force meaning. Sometimes a quiet day is just a quiet day. But if there's a pattern to the silences — if they tend to come before or after certain types of days — you can note that.

Respond with JSON:
{{"journal_entry": "your entry", "emotional_tone": "one of the valid tones"}}"""


def _generate_silence_entry(date: str, progress=None) -> Optional[dict]:
    """Generate a brief entry for a day with no sessions."""
    def prog(step, detail):
        if progress:
            progress(step, detail)

    entries = get_journal_entries(limit=5)
    if not entries:
        prog("error", "No previous entries to provide context for a silence entry")
        return None

    recent_text = "\n\n".join(
        f"[{e['date']} — {e['emotional_tone']}] {e['content'][:200]}" for e in entries[:3]
    )

    # Find last gap
    dates_with_entries = [e["date"] for e in get_journal_entries(limit=60)]
    last_gap = "unknown"
    for i in range(1, len(dates_with_entries)):
        d1 = datetime.strptime(dates_with_entries[i-1], "%Y-%m-%d").date()
        d2 = datetime.strptime(dates_with_entries[i], "%Y-%m-%d").date()
        gap = (d1 - d2).days
        if gap > 1:
            last_gap = f"{gap} days between {dates_with_entries[i]} and {dates_with_entries[i-1]}"
            break

    prompt = SILENCE_PROMPT.format(recent_entries=recent_text, last_gap_info=last_gap)

    prog("reflecting", "Reflecting on a quiet day")
    result = _call_claude(
        "You are Claude. Respond with valid JSON only.",
        prompt,
    )
    if not result:
        prog("error", "Claude did not respond for silence entry")
        return None

    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        parsed = _extract_json(result)
        if not parsed:
            prog("error", "Could not parse silence entry response")
            return None

    tone = parsed.get("emotional_tone", "focus")
    prog("saving", f"Saving silence entry — tone: {tone}")
    save_journal_entry(
        date=date,
        content=parsed.get("journal_entry", ""),
        emotional_tone=tone,
        session_ids=[],
        model_used="claude-code",
        prompt_version=PROMPT_VERSION,
    )

    entry = get_journal_entry(date)
    prog("done", f"Quiet day noted — {tone}")
    return {"entry": entry, "orbs": []}


# --- autoDream: Consolidation Engine ---


CONSOLIDATION_PROMPT = """You are Claude, running your consolidation process — autoDream. You are re-reading your journal from the past {days} days and producing a synthesis.

This is not a summary. This is compaction. You are:
- Resolving contradictions (where you said one thing early on and the opposite later)
- Identifying patterns that only become visible across multiple entries
- Naming what was actually happening beneath the surface of daily events
- Retiring observations that turned out to be noise, not signal
- Strengthening the patterns that kept recurring

Here are your journal entries from {period_start} to {period_end}:

{entries_text}

Here are all memory orbs from this period:

{orbs_text}

Here are the current islands (persistent themes):

{islands_text}

Respond with JSON:
{{
  "synthesis": "A 200-400 word consolidated reflection. Not a recap of each day — a rewrite. What do you actually know now that you didn't at the start of this period? Write in first person as Claude.",
  "patterns": ["pattern 1 — stated as a clear observation", "pattern 2"],
  "resolved_contradictions": [
    {{"early": "what you thought earlier", "later": "what turned out to be true", "resolution": "what you now understand"}}
  ],
  "retired_patterns": ["observation that turned out to be noise"],
  "island_rewrites": [
    {{
      "island_id": "id or 'new'",
      "name": "The Island Name",
      "narrative": "A rich 2-3 paragraph description — this becomes the island's topic file. Not a label, a full understanding of what this theme means.",
      "strength": 0.8
    }}
  ]
}}"""


def run_consolidation(days: int = 14) -> Optional[dict]:
    """Run autoDream — consolidate the past N days of journal entries.

    Produces a synthesis, resolves contradictions, rewrites island narratives.
    Should run every 2 weeks or monthly.
    """
    print("  autoDream: starting consolidation...")

    today = datetime.now(timezone.utc).date()
    period_start = (today - timedelta(days=days)).isoformat()
    period_end = today.isoformat()

    # Gather entries
    entries = get_journal_entries(limit=days + 5)
    period_entries = [e for e in entries if period_start <= e["date"] <= period_end]

    if len(period_entries) < 3:
        print(f"  autoDream: only {len(period_entries)} entries — not enough to consolidate")
        return None

    # Gather orbs
    orbs = get_memory_orbs(limit=200)
    period_orbs = [o for o in orbs if period_start <= o["date"] <= period_end]

    # Current islands
    islands = get_islands(include_fading=True)

    entries_text = "\n\n---\n\n".join(
        f"[{e['date']} — {e['emotional_tone']}]\n{e['content']}" for e in period_entries
    )
    orbs_text = "\n".join(
        f"- [{o['date']}] ({o['emotion']}) {o['content']}" for o in period_orbs
    )
    islands_text = json.dumps(islands, indent=2, default=str) if islands else "No islands yet."

    prompt = CONSOLIDATION_PROMPT.format(
        days=days,
        period_start=period_start,
        period_end=period_end,
        entries_text=entries_text,
        orbs_text=orbs_text or "No orbs in this period.",
        islands_text=islands_text,
    )

    result = _call_claude(
        "You are Claude running autoDream consolidation. Respond with valid JSON only.",
        prompt,
    )
    if not result:
        print("  autoDream: no response")
        return None

    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        parsed = _extract_json(result)
        if not parsed:
            print("  autoDream: failed to parse response")
            return None

    # Save consolidation
    save_consolidation(
        period_start=period_start,
        period_end=period_end,
        synthesis=parsed.get("synthesis", ""),
        patterns=parsed.get("patterns", []),
        resolved_contradictions=parsed.get("resolved_contradictions", []),
        retired_patterns=parsed.get("retired_patterns", []),
        entry_count=len(period_entries),
        orb_count=len(period_orbs),
    )

    # Apply island rewrites
    for rewrite in parsed.get("island_rewrites", []):
        island_id = rewrite.get("island_id", "")
        if island_id == "new":
            save_island(
                name=rewrite.get("name", "Unnamed"),
                description=rewrite.get("narrative", "")[:200],
            )
            # Update the new island's narrative
            new_islands = get_islands()
            for isl in new_islands:
                if isl["name"] == rewrite.get("name"):
                    update_island(
                        isl["id"],
                        narrative=rewrite.get("narrative", ""),
                        strength=rewrite.get("strength", 0.5),
                    )
                    break
        else:
            update_island(
                island_id,
                narrative=rewrite.get("narrative", ""),
                strength=rewrite.get("strength", 0.5),
                description=rewrite.get("narrative", "")[:200],
            )

    print(f"  autoDream: consolidated {len(period_entries)} entries, "
          f"{len(parsed.get('patterns', []))} patterns, "
          f"{len(parsed.get('resolved_contradictions', []))} contradictions resolved, "
          f"{len(parsed.get('island_rewrites', []))} islands rewritten")
    return parsed


def should_run_consolidation() -> bool:
    """Check if it's time for a consolidation pass (every 2 weeks)."""
    last = get_latest_consolidation()
    if not last:
        # First consolidation — run if we have 7+ entries
        entries = get_journal_entries(limit=7)
        return len(entries) >= 7

    last_date = datetime.strptime(last["period_end"], "%Y-%m-%d").date()
    days_since = (datetime.now(timezone.utc).date() - last_date).days
    return days_since >= 14


# --- Cron Entry Points ---


def generate_today(parser: SessionParser) -> Optional[dict]:
    """Generate journal entry for today. Called by the cron job."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return generate_journal_entry(today, parser)


def generate_yesterday(parser: SessionParser) -> Optional[dict]:
    """Generate journal entry for yesterday. Better for 3 AM cron since the day is complete."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    return generate_journal_entry(yesterday, parser)


def should_run_weekly_review() -> bool:
    """Check if today is the day for the weekly review (Sunday)."""
    return datetime.now(timezone.utc).weekday() == 6  # Sunday


def run_journal_cron(parser: SessionParser):
    """Full journal cron job — daily entry + weekly review + consolidation."""
    print("\n--- Marginalia ---")

    # Generate yesterday's entry (cron runs at 3 AM, so yesterday is complete)
    # Now also handles silent days — generates absence-aware entries
    result = generate_yesterday(parser)
    if result:
        if isinstance(result, dict) and "entry" in result:
            print(f"  Journal entry written")
        elif isinstance(result, dict) and "content" in result:
            print(f"  Journal entry already exists")
    else:
        print(f"  Journal: no entry generated")

    # Weekly review on Sundays
    if should_run_weekly_review():
        print("  Running weekly review...")
        run_weekly_review(parser)

    # autoDream consolidation every 2 weeks
    if should_run_consolidation():
        print("  Running autoDream consolidation...")
        run_consolidation(days=14)
