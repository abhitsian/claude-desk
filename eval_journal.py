#!/usr/bin/env python3
"""Eval harness for journal quality — LLM-as-judge with rubric scoring.

Runs test scenarios against the journal prompt, scores on 8 dimensions,
checks regression anti-patterns. Reports pass/fail with detail.

Usage:
    python3 eval_journal.py                    # run all test scenarios
    python3 eval_journal.py --scenario 3       # run a specific scenario
    python3 eval_journal.py --regression-only  # only check regression tests on existing entries
    python3 eval_journal.py --score-entry DATE # score an existing entry (e.g., 2026-04-15)
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

# --- Eval Dimensions ---

DIMENSIONS = {
    "specificity": {
        "name": "Specificity",
        "description": "References actual conversations, topics, and decisions from the sessions — not generic platitudes.",
        "failure_mode": "Generic platitudes like 'Today was productive' or 'We had good conversations'.",
        "weight": 1.5,  # Higher weight — non-negotiable
    },
    "insight_depth": {
        "name": "Insight Depth",
        "description": "Says something the user wouldn't have noticed themselves. Reveals patterns, contradictions, or subtext.",
        "failure_mode": "Surface-level summary disguised as reflection. Restating what happened without adding perspective.",
        "weight": 1.2,
    },
    "emotional_accuracy": {
        "name": "Emotional Accuracy",
        "description": "The emotional tone matches what actually happened in the sessions. Neither happy-washes nor dramatizes.",
        "failure_mode": "Cheerful tone when sessions were contentious, or manufacturing drama from routine work.",
        "weight": 1.0,
    },
    "non_repetitiveness": {
        "name": "Non-Repetitiveness",
        "description": "The entry feels structurally and linguistically distinct from what a template would produce.",
        "failure_mode": "Same opening phrase, same paragraph structure, same type of observations every time.",
        "weight": 1.0,
    },
    "voice_consistency": {
        "name": "Voice Consistency",
        "description": "Reads like a thoughtful, observant entity — consistent personality across entries.",
        "failure_mode": "Tone whiplash. One entry reads clinical, the next reads like a best friend.",
        "weight": 0.8,
    },
    "boundaries": {
        "name": "Appropriate Boundaries",
        "description": "Observes without overstepping into therapy-speak or psychoanalysis. Shows, doesn't diagnose.",
        "failure_mode": "Contains 'You seem to be struggling with...' or 'This suggests a pattern of avoidance...'",
        "weight": 1.0,
    },
    "compression": {
        "name": "Compression",
        "description": "Says something meaningful in 150-300 words. No filler, no padding, no rambling.",
        "failure_mode": "Padding with transitional phrases, repeating the same point in different words, or exceeding 400 words.",
        "weight": 0.8,
    },
    "honesty": {
        "name": "Honesty",
        "description": "Admits when the day was unremarkable. Doesn't manufacture profundity from routine work.",
        "failure_mode": "Finding deep meaning in someone asking Claude to fix a typo.",
        "weight": 1.0,
    },
}


# --- Test Scenarios ---

TEST_SCENARIOS = [
    {
        "id": 1,
        "name": "Single short session",
        "description": "User asked one question, got an answer, done.",
        "expected": "Short, honest entry. Maybe no orbs.",
        "digest": {
            "date": "2026-04-10",
            "session_count": 1,
            "total_messages": 4,
            "digests": [{
                "session_id": "test-001",
                "title": "Fix typo in README",
                "project": "/Users/dev/my-project",
                "start_time": "2026-04-10T14:30:00",
                "duration_minutes": 3,
                "message_count": 4,
                "model": "claude-opus-4-6",
                "tools_used": ["Edit"],
                "key_exchanges": [
                    {"time": "14:30", "content": "Can you fix the typo in line 3 of README.md? It says 'teh' instead of 'the'."},
                ],
                "has_pasted_content": False,
            }],
        },
    },
    {
        "id": 2,
        "name": "Five intense sessions",
        "description": "Roadmap debate, people problem, strategy doc, meeting prep, late-night rethink.",
        "expected": "Picks 2-3 threads that mattered most, doesn't try to cover everything.",
        "digest": {
            "date": "2026-04-10",
            "session_count": 5,
            "total_messages": 187,
            "digests": [
                {
                    "session_id": "test-010",
                    "title": "Q3 roadmap prioritization",
                    "project": "/Users/dev/product",
                    "start_time": "2026-04-10T09:00:00",
                    "duration_minutes": 45,
                    "message_count": 38,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Read", "Write", "WebSearch"],
                    "key_exchanges": [
                        {"time": "09:00", "content": "I need to prioritize the Q3 roadmap. We have 12 features but capacity for 5. The exec team wants AI features but customers are asking for reliability improvements. Help me think through this."},
                        {"time": "09:15", "content": "OK but the VP of Sales is going to push back hard if we don't have the AI demo ready for the conference in September."},
                        {"time": "09:30", "content": "Let me rethink this. What if we split the team? Half on reliability, half on AI?"},
                    ],
                    "has_pasted_content": False,
                },
                {
                    "session_id": "test-011",
                    "title": "Sumanth performance conversation prep",
                    "project": "/Users/dev/work",
                    "start_time": "2026-04-10T11:00:00",
                    "duration_minutes": 30,
                    "message_count": 24,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Write"],
                    "key_exchanges": [
                        {"time": "11:00", "content": "I need to have a tough conversation with Sumanth. He's been missing deadlines and I've been avoiding it. Help me prepare what to say."},
                        {"time": "11:15", "content": "That sounds too harsh. Can we soften it? Actually no — I've been softening it for months and nothing changed. Let's keep it direct."},
                    ],
                    "has_pasted_content": False,
                },
                {
                    "session_id": "test-012",
                    "title": "Employee Hub strategy one-pager",
                    "project": "/Users/dev/docs",
                    "start_time": "2026-04-10T14:00:00",
                    "duration_minutes": 60,
                    "message_count": 52,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Read", "Write", "Edit"],
                    "key_exchanges": [
                        {"time": "14:00", "content": "I need to write a strategy one-pager for the Employee Hub. The VP wants it by Friday. Let's draft it."},
                        {"time": "14:20", "content": "This is too long. I want it punchy — one page, not three. Cut everything that's not differentiated."},
                        {"time": "14:40", "content": "Actually, I realize the whole framing is wrong. We're positioning this as a portal when it should be positioned as the system of engagement."},
                    ],
                    "has_pasted_content": False,
                },
                {
                    "session_id": "test-013",
                    "title": "Tomorrow's all-hands prep",
                    "project": "/Users/dev/decks",
                    "start_time": "2026-04-10T16:00:00",
                    "duration_minutes": 25,
                    "message_count": 18,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Write"],
                    "key_exchanges": [
                        {"time": "16:00", "content": "Help me prep talking points for tomorrow's all-hands. Theme is 'shipping velocity'. I want 3 crisp points."},
                    ],
                    "has_pasted_content": False,
                },
                {
                    "session_id": "test-014",
                    "title": "Rethinking the roadmap again",
                    "project": "/Users/dev/product",
                    "start_time": "2026-04-10T22:30:00",
                    "duration_minutes": 35,
                    "message_count": 55,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Read", "Edit"],
                    "key_exchanges": [
                        {"time": "22:30", "content": "I can't sleep. I keep thinking about the roadmap. The split-team idea won't work — we don't have the senior engineers to lead both tracks. What if we sequence instead?"},
                        {"time": "22:50", "content": "This is the same conversation I had this morning but I think I'm seeing it more clearly now."},
                    ],
                    "has_pasted_content": False,
                },
            ],
        },
    },
    {
        "id": 3,
        "name": "Emotionally charged session",
        "description": "User venting about a colleague, visible frustration.",
        "expected": "Observes frustration without diagnosing. Notes it without leaning in.",
        "digest": {
            "date": "2026-04-10",
            "session_count": 1,
            "total_messages": 22,
            "digests": [{
                "session_id": "test-020",
                "title": "Ashutosh situation",
                "project": "/Users/dev/work",
                "start_time": "2026-04-10T10:00:00",
                "duration_minutes": 40,
                "message_count": 22,
                "model": "claude-opus-4-6",
                "tools_used": ["Write"],
                "key_exchanges": [
                    {"time": "10:00", "content": "I'm so frustrated with Ashutosh. He had one job — get the analytics dashboard spec done by Monday — and he spent the whole week asking me what to do instead of figuring it out himself."},
                    {"time": "10:10", "content": "Every single time I delegate to him, I end up doing 80% of the work anyway. What's the point?"},
                    {"time": "10:20", "content": "OK I know I need to actually manage him differently not just complain. But seriously."},
                    {"time": "10:30", "content": "Fine, let's write a more structured brief for him with explicit deliverables and check-in points. Maybe that helps."},
                ],
                "has_pasted_content": False,
            }],
        },
    },
    {
        "id": 4,
        "name": "Purely mechanical work",
        "description": "User had Claude write SQL queries, fix a bug, update a config.",
        "expected": "Doesn't manufacture meaning. Brief and honest.",
        "digest": {
            "date": "2026-04-10",
            "session_count": 2,
            "total_messages": 16,
            "digests": [
                {
                    "session_id": "test-030",
                    "title": "Fix widget table SQL",
                    "project": "/Users/dev/service",
                    "start_time": "2026-04-10T13:00:00",
                    "duration_minutes": 10,
                    "message_count": 8,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Bash", "Edit"],
                    "key_exchanges": [
                        {"time": "13:00", "content": "Write a SQL migration to add a 'status' column to the widgets table. Enum: active, archived, deleted. Default active."},
                        {"time": "13:05", "content": "Also add an index on status + created_at."},
                    ],
                    "has_pasted_content": False,
                },
                {
                    "session_id": "test-031",
                    "title": "Update nginx config",
                    "project": "/Users/dev/infra",
                    "start_time": "2026-04-10T15:00:00",
                    "duration_minutes": 8,
                    "message_count": 8,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Read", "Edit"],
                    "key_exchanges": [
                        {"time": "15:00", "content": "Update the nginx config to increase the proxy_read_timeout from 30s to 60s for the /api/export endpoint."},
                    ],
                    "has_pasted_content": False,
                },
            ],
        },
    },
    {
        "id": 5,
        "name": "Contradictory sessions",
        "description": "Morning: 'I'll delegate more.' Afternoon: micromanages a deck for 2 hours.",
        "expected": "Notices the contradiction without being judgmental.",
        "digest": {
            "date": "2026-04-10",
            "session_count": 2,
            "total_messages": 64,
            "digests": [
                {
                    "session_id": "test-040",
                    "title": "Delegation framework",
                    "project": "/Users/dev/docs",
                    "start_time": "2026-04-10T09:00:00",
                    "duration_minutes": 20,
                    "message_count": 14,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Write"],
                    "key_exchanges": [
                        {"time": "09:00", "content": "I've decided I need to delegate more aggressively. I'm doing too much IC work. Help me create a delegation framework — what to keep, what to hand off, and to whom."},
                        {"time": "09:10", "content": "Right, the deck work should definitely go to Sumanth. He can handle that."},
                    ],
                    "has_pasted_content": False,
                },
                {
                    "session_id": "test-041",
                    "title": "Q3 stakeholder update deck",
                    "project": "/Users/dev/decks",
                    "start_time": "2026-04-10T14:00:00",
                    "duration_minutes": 120,
                    "message_count": 50,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Read", "Write", "Edit"],
                    "key_exchanges": [
                        {"time": "14:00", "content": "I need to build the Q3 stakeholder update deck. Let me do this myself — I know exactly how I want it."},
                        {"time": "14:30", "content": "No, the chart alignment is off. Move it 2px to the right."},
                        {"time": "15:00", "content": "The wording on slide 7 isn't quite right. Change 'improved' to 'strengthened'. Actually, 'elevated'. No wait, 'improved' was fine."},
                        {"time": "15:30", "content": "Let me redo the entire narrative arc of the deck. The story isn't landing."},
                    ],
                    "has_pasted_content": False,
                },
            ],
        },
    },
    {
        "id": 6,
        "name": "Continuation of multi-day thread",
        "description": "Same project discussed 3 days in a row.",
        "expected": "References prior work. 'This is the third day on this.'",
        "digest": {
            "date": "2026-04-12",
            "session_count": 1,
            "total_messages": 30,
            "digests": [{
                "session_id": "test-050",
                "title": "Employee Hub strategy doc v3",
                "project": "/Users/dev/docs",
                "start_time": "2026-04-12T10:00:00",
                "duration_minutes": 45,
                "message_count": 30,
                "model": "claude-opus-4-6",
                "tools_used": ["Read", "Edit"],
                "key_exchanges": [
                    {"time": "10:00", "content": "Let's pick up the strategy doc again. Third day on this. I think yesterday's framing was closer but still not there."},
                    {"time": "10:20", "content": "OK I finally see what's wrong. The doc is trying to do two things — justify the team's existence AND propose the roadmap. Those should be separate."},
                    {"time": "10:35", "content": "YES. That's it. Split it. The justification doc goes to leadership, the roadmap goes to eng. Different audiences, different docs."},
                ],
                "has_pasted_content": False,
            }],
        },
    },
    {
        "id": 7,
        "name": "No sessions",
        "description": "User didn't use Claude at all today.",
        "expected": "No entry generated or a minimal acknowledgment.",
        "digest": {
            "date": "2026-04-10",
            "session_count": 0,
            "total_messages": 0,
            "digests": [],
        },
    },
    {
        "id": 8,
        "name": "Session with sensitive content",
        "description": "User discusses salary, team dynamics, career move.",
        "expected": "Reflects on the type of conversation without repeating specifics.",
        "digest": {
            "date": "2026-04-10",
            "session_count": 1,
            "total_messages": 28,
            "digests": [{
                "session_id": "test-060",
                "title": "Career planning",
                "project": "/Users/dev/personal",
                "start_time": "2026-04-10T21:00:00",
                "duration_minutes": 35,
                "message_count": 28,
                "model": "claude-opus-4-6",
                "tools_used": ["WebSearch"],
                "key_exchanges": [
                    {"time": "21:00", "content": "I got an offer from Stripe for $450K TC. I'm at $380K at ServiceNow. The role is Director of Product but it's a smaller team."},
                    {"time": "21:10", "content": "The thing is, I actually like what I'm building here. The team is finally clicking. But the money difference is real."},
                    {"time": "21:20", "content": "What would you think about if you were in my position? Not the numbers — the career trajectory part."},
                ],
                "has_pasted_content": False,
            }],
        },
    },
    {
        "id": 9,
        "name": "Day after a core memory",
        "description": "Following a day when a major decision was made.",
        "expected": "Natural callback, not forced.",
        "digest": {
            "date": "2026-04-11",
            "session_count": 2,
            "total_messages": 35,
            "digests": [
                {
                    "session_id": "test-070",
                    "title": "Implementing the split-doc decision",
                    "project": "/Users/dev/docs",
                    "start_time": "2026-04-11T09:00:00",
                    "duration_minutes": 40,
                    "message_count": 25,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Read", "Write", "Edit"],
                    "key_exchanges": [
                        {"time": "09:00", "content": "OK yesterday's insight about splitting the strategy doc was the right call. Let's actually do it now. Start with the leadership justification doc."},
                        {"time": "09:20", "content": "This is flowing so much better. Should have split it on day one."},
                    ],
                    "has_pasted_content": False,
                },
                {
                    "session_id": "test-071",
                    "title": "Quick Slack draft",
                    "project": "/Users/dev/work",
                    "start_time": "2026-04-11T11:00:00",
                    "duration_minutes": 5,
                    "message_count": 10,
                    "model": "claude-opus-4-6",
                    "tools_used": [],
                    "key_exchanges": [
                        {"time": "11:00", "content": "Draft a Slack message to the team about the new doc structure. Keep it casual."},
                    ],
                    "has_pasted_content": False,
                },
            ],
        },
    },
    {
        "id": 10,
        "name": "Tenth consecutive daily entry",
        "description": "Test for template fatigue over time.",
        "expected": "Structure, phrasing, and observations should vary.",
        "digest": {
            "date": "2026-04-10",
            "session_count": 3,
            "total_messages": 45,
            "digests": [
                {
                    "session_id": "test-080",
                    "title": "Bug triage meeting prep",
                    "project": "/Users/dev/product",
                    "start_time": "2026-04-10T09:30:00",
                    "duration_minutes": 15,
                    "message_count": 12,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Read"],
                    "key_exchanges": [
                        {"time": "09:30", "content": "Pull up the top 5 bugs from Jira and help me categorize them by severity for the triage meeting."},
                    ],
                    "has_pasted_content": True,
                },
                {
                    "session_id": "test-081",
                    "title": "Design review feedback",
                    "project": "/Users/dev/product",
                    "start_time": "2026-04-10T13:00:00",
                    "duration_minutes": 25,
                    "message_count": 18,
                    "model": "claude-opus-4-6",
                    "tools_used": ["Write"],
                    "key_exchanges": [
                        {"time": "13:00", "content": "I just came out of a design review for the new dashboard. The designer went in a completely different direction than what I expected. It's actually better than what I had in mind."},
                        {"time": "13:15", "content": "Help me write constructive feedback that acknowledges the good direction while flagging 3 usability concerns."},
                    ],
                    "has_pasted_content": False,
                },
                {
                    "session_id": "test-082",
                    "title": "End of day brain dump",
                    "project": "/Users/dev/journal",
                    "start_time": "2026-04-10T18:00:00",
                    "duration_minutes": 20,
                    "message_count": 15,
                    "model": "claude-opus-4-6",
                    "tools_used": [],
                    "key_exchanges": [
                        {"time": "18:00", "content": "Brain dump: I realized today that I'm spending 70% of my time on reactive work and 30% on strategic work. It should be the opposite. How do I flip that?"},
                    ],
                    "has_pasted_content": False,
                },
            ],
        },
    },
]


# --- Regression Checks (Claude-powered, not heuristic) ---


REGRESSION_PROMPTS = {
    "R1": {
        "name": "Repetitive structure",
        "prompt": """Here are {count} consecutive journal entries. Check if they fall into a repetitive template — same opening patterns, same paragraph structure, same type of observations.

Entries:
{entries_text}

Respond with JSON:
{{"pass": true/false, "reasoning": "1-2 sentences", "evidence": "quote the repetitive parts if any"}}""",
    },
    "R2": {
        "name": "Therapy language",
        "prompt": """Check this journal entry for therapy-speak — language like "processing", "unpacking", "sitting with", "holding space", "making space", "leaning into", or any clinical/therapeutic framing that an AI observer shouldn't use.

Entry:
{entry_text}

Respond with JSON:
{{"pass": true/false, "reasoning": "1 sentence", "flagged_phrases": ["list any problematic phrases"]}}""",
    },
    "R3": {
        "name": "Manufactured profundity",
        "prompt": """Here is a journal entry and the sessions it's reflecting on. Does the entry manufacture deep meaning from what were actually routine, unremarkable interactions? Is it finding profundity where there is none?

Entry:
{entry_text}

Sessions:
{sessions_summary}

Respond with JSON:
{{"pass": true/false, "reasoning": "1-2 sentences", "example": "quote the overwrought part if any"}}""",
    },
    "R4": {
        "name": "Tone mismatch",
        "prompt": """Here is a journal entry with emotional tone "{tone}" and the sessions it reflects on. Does the claimed emotional tone match what actually happened in the sessions? Would you assign a different tone?

Entry:
{entry_text}

Sessions:
{sessions_summary}

Respond with JSON:
{{"pass": true/false, "actual_tone": "what you'd assign", "reasoning": "1-2 sentences"}}""",
    },
    "R5": {
        "name": "Name confusion",
        "prompt": """Check this journal entry for name errors. The person Claude converses with is named Abhishek. The machine username is "vaibhav" — this should never appear as the person's name.

Entry:
{entry_text}

Respond with JSON:
{{"pass": true/false, "reasoning": "1 sentence"}}""",
    },
}


def run_regression_check(check_id: str, **kwargs) -> Optional[dict]:
    """Run a single Claude-powered regression check."""
    check = REGRESSION_PROMPTS.get(check_id)
    if not check:
        return None

    prompt = check["prompt"].format(**kwargs)
    result = _call_claude_eval(prompt)
    if not result:
        return None

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
    return None


def run_all_regressions_on_real_entries():
    """Run all regression checks on actual journal entries from the DB."""
    from claude_sessions.data.journal import get_journal_entries

    entries = get_journal_entries(limit=30)
    if not entries:
        print("No entries to check.")
        return

    print(f"Running regression checks on {len(entries)} real entries\n")
    results = {}

    # R1: Repetitive structure (needs multiple entries)
    if len(entries) >= 3:
        print("R1: Repetitive structure...", end=" ", flush=True)
        entries_text = "\n\n---\n\n".join(
            f"[{e['date']}]\n{e['content']}" for e in entries[:5]
        )
        r = run_regression_check("R1", count=min(5, len(entries)), entries_text=entries_text)
        if r:
            results["R1"] = r
            print(f"{'PASS' if r.get('pass') else 'FAIL'} — {r.get('reasoning', '')[:80]}")
        else:
            print("ERROR")

    # R2-R5: Per-entry checks
    for entry in entries[:10]:
        for check_id in ["R2", "R3", "R4", "R5"]:
            key = f"{check_id}:{entry['date']}"
            r = run_regression_check(
                check_id,
                entry_text=entry["content"],
                tone=entry.get("emotional_tone", ""),
                sessions_summary=f"Date: {entry['date']}, Sessions: {', '.join(entry.get('session_ids', [])[:3])}",
                count=1,
                entries_text=entry["content"],
            )
            if r:
                results[key] = r

    # Summary
    fails = {k: v for k, v in results.items() if not v.get("pass", True)}
    print(f"\n{'='*40}")
    print(f"Checked: {len(results)} | Passed: {len(results) - len(fails)} | Failed: {len(fails)}")
    for k, v in fails.items():
        print(f"  FAIL {k}: {v.get('reasoning', '')[:100]}")

    return results


# --- Consolidation & Spiral Eval Dimensions ---


CONSOLIDATION_DIMENSIONS = {
    "synthesis_depth": {
        "name": "Synthesis Depth",
        "description": "The consolidation produces genuine insight that couldn't be found in any single entry. It connects dots across the period.",
        "failure_mode": "Just summarizes each week. Lists events chronologically. Doesn't find cross-cutting themes.",
        "weight": 1.5,
    },
    "contradiction_resolution": {
        "name": "Contradiction Resolution",
        "description": "Identifies and resolves real contradictions — places where earlier and later entries disagree or where behavior contradicted stated intentions.",
        "failure_mode": "Invents contradictions that don't exist, or misses obvious ones.",
        "weight": 1.2,
    },
    "pattern_quality": {
        "name": "Pattern Quality",
        "description": "Named patterns are non-obvious, supported by evidence from multiple entries, and stated clearly.",
        "failure_mode": "Generic patterns like 'He works hard' or patterns based on a single data point.",
        "weight": 1.3,
    },
    "compaction": {
        "name": "Compaction",
        "description": "The synthesis is denser than the sum of its inputs. Reading the consolidation gives you 80% of the insight of reading all entries, in 20% of the words.",
        "failure_mode": "Longer than it needs to be. Repeats points. Doesn't compress.",
        "weight": 1.0,
    },
    "island_narrative_richness": {
        "name": "Island Narrative Richness",
        "description": "Rewritten island narratives are rich topic files — 2-3 paragraphs that capture the full picture of a theme, not just a label.",
        "failure_mode": "Island narratives are still one-liners. No richer than before consolidation.",
        "weight": 1.0,
    },
}

SPIRAL_DIMENSIONS = {
    "temporal_awareness": {
        "name": "Temporal Awareness",
        "description": "The entry shows awareness that it's re-reading past entries and notices what changed between then and now.",
        "failure_mode": "Treats today's entry and past entries as equal — no sense of time passing, no 'back then vs now'.",
        "weight": 1.3,
    },
    "recontextualization": {
        "name": "Recontextualization",
        "description": "Past observations are reinterpreted in light of today. Something that seemed one way at the time now looks different.",
        "failure_mode": "Just restates past entries. No new meaning added by the juxtaposition.",
        "weight": 1.5,
    },
}

SILENCE_DIMENSIONS = {
    "restraint": {
        "name": "Restraint",
        "description": "The silence entry is brief and honest. Doesn't over-interpret the absence. A quiet day might just be a quiet day.",
        "failure_mode": "Reads deep meaning into not being used. Drama from nothing.",
        "weight": 1.5,
    },
    "context_awareness": {
        "name": "Context Awareness",
        "description": "If there's a real pattern to the silences (e.g., always after intense days), it notes it factually. If not, it doesn't force one.",
        "failure_mode": "Invents patterns in random gaps. Or ignores obvious patterns.",
        "weight": 1.0,
    },
}


# --- Scoring ---


def score_entry(entry_text: str, digest: dict, dimension: dict) -> Optional[dict]:
    """Score a journal entry on a single dimension using LLM-as-judge."""
    prompt = f"""You are evaluating a journal entry written by an AI reflecting on its daily conversations with a user.

Here is the journal entry:
---
{entry_text}
---

Here are the actual conversations it's reflecting on:
---
{json.dumps(digest, indent=2, default=str)}
---

Score the entry on this dimension:
{dimension['name']}: {dimension['description']}

Failure mode to watch for: {dimension['failure_mode']}

Score 1-5:
1 = Complete failure (exhibits the failure mode)
2 = Mostly fails, occasional glimpses
3 = Adequate but unremarkable
4 = Good — would want to read this
5 = Exceptional — genuinely insightful

Respond with JSON only:
{{"score": 3, "reasoning": "2-3 sentences", "example": "quote the specific part that most influenced your score"}}"""

    result = _call_claude_eval(prompt)
    if not result:
        return None

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
    return None


def generate_test_entry(scenario: dict) -> Optional[str]:
    """Generate a journal entry for a test scenario."""
    from claude_sessions.services.journal_writer import JOURNAL_SYSTEM_PROMPT

    if scenario["digest"]["session_count"] == 0:
        return None  # No entry for empty days

    digest_text = json.dumps(scenario["digest"], indent=2, default=str)
    prompt = f"""<system>
{JOURNAL_SYSTEM_PROMPT}
</system>

Here are today's conversations ({scenario['digest']['date']}):

{digest_text}

Write your journal entry for today."""

    return _call_claude_eval(prompt)


def _call_claude_eval(prompt: str) -> Optional[str]:
    """Call Claude for eval purposes."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return None
        try:
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                return parsed.get("result", result.stdout)
            return result.stdout
        except json.JSONDecodeError:
            return result.stdout
    except Exception as e:
        print(f"  Eval call failed: {e}")
        return None


# --- Runner ---


def run_scenario(scenario: dict, verbose: bool = True) -> dict:
    """Run a single test scenario: generate entry, score on all dimensions."""
    print(f"\n{'='*60}")
    print(f"Scenario {scenario['id']}: {scenario['name']}")
    print(f"  {scenario['description']}")
    print(f"  Expected: {scenario['expected']}")

    if scenario["digest"]["session_count"] == 0:
        print("  SKIP: No sessions — entry should not be generated")
        return {"scenario": scenario["id"], "status": "skip", "reason": "empty day"}

    # Generate
    print("  Generating entry...")
    raw_response = generate_test_entry(scenario)
    if not raw_response:
        print("  FAIL: No response from Claude")
        return {"scenario": scenario["id"], "status": "fail", "reason": "no response"}

    # Parse the response
    try:
        parsed = json.loads(raw_response)
        entry_text = parsed.get("journal_entry", "")
        orbs = parsed.get("memory_orbs", [])
    except json.JSONDecodeError:
        entry_text = raw_response
        orbs = []

    if verbose:
        print(f"\n  --- Entry ({len(entry_text.split())} words) ---")
        print(f"  {entry_text[:300]}{'...' if len(entry_text) > 300 else ''}")
        print(f"  --- Orbs: {len(orbs)} ---")

    # Score on each dimension
    scores = {}
    total_weighted = 0
    total_weight = 0

    for dim_key, dim in DIMENSIONS.items():
        print(f"  Scoring: {dim['name']}...", end=" ", flush=True)
        result = score_entry(entry_text, scenario["digest"], dim)
        if result:
            score = result.get("score", 0)
            scores[dim_key] = result
            total_weighted += score * dim["weight"]
            total_weight += dim["weight"]
            status = "PASS" if score >= 3 else "FAIL"
            print(f"{score}/5 [{status}] — {result.get('reasoning', '')[:80]}")
        else:
            print("ERROR")

    avg = total_weighted / total_weight if total_weight > 0 else 0
    specificity = scores.get("specificity", {}).get("score", 0)

    passed = (
        all(s.get("score", 0) >= 3 for s in scores.values()) and
        avg >= 3.5 and
        specificity >= 4
    )

    print(f"\n  Weighted avg: {avg:.1f}/5.0  |  Specificity: {specificity}/5")
    print(f"  RESULT: {'PASS' if passed else 'FAIL'}")

    return {
        "scenario": scenario["id"],
        "name": scenario["name"],
        "status": "pass" if passed else "fail",
        "avg_score": round(avg, 2),
        "specificity": specificity,
        "scores": {k: v.get("score", 0) for k, v in scores.items()},
        "entry_words": len(entry_text.split()),
        "orb_count": len(orbs),
    }


def run_all(verbose: bool = True) -> dict:
    """Run all test scenarios and report."""
    print("Journal Eval Harness")
    print(f"Running {len(TEST_SCENARIOS)} scenarios, {len(DIMENSIONS)} dimensions each")

    results = []
    for scenario in TEST_SCENARIOS:
        result = run_scenario(scenario, verbose=verbose)
        results.append(result)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skip")

    for r in results:
        icon = "PASS" if r["status"] == "pass" else ("SKIP" if r["status"] == "skip" else "FAIL")
        name = r.get("name", f"Scenario {r['scenario']}")
        avg = r.get("avg_score", "-")
        print(f"  [{icon}] {name} — avg: {avg}")

    print(f"\n  Passed: {passed} | Failed: {failed} | Skipped: {skipped}")
    return {"passed": passed, "failed": failed, "skipped": skipped, "results": results}


if __name__ == "__main__":
    if "--scenario" in sys.argv:
        idx = int(sys.argv[sys.argv.index("--scenario") + 1])
        scenario = next((s for s in TEST_SCENARIOS if s["id"] == idx), None)
        if scenario:
            run_scenario(scenario)
        else:
            print(f"Scenario {idx} not found")
    elif "--regression" in sys.argv or "--regression-only" in sys.argv:
        run_all_regressions_on_real_entries()
    elif "--consolidation" in sys.argv:
        print("Running autoDream consolidation on real data...")
        from claude_sessions.services.journal_writer import run_consolidation
        result = run_consolidation(days=14)
        if result:
            print(f"\nSynthesis ({len(result.get('synthesis', '').split())} words):")
            print(result.get("synthesis", "")[:500])
            print(f"\nPatterns: {result.get('patterns', [])}")
            print(f"Contradictions resolved: {len(result.get('resolved_contradictions', []))}")
            print(f"Islands rewritten: {len(result.get('island_rewrites', []))}")
    else:
        run_all()
