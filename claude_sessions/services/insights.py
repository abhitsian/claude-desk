"""Cross-conversation intelligence engine.

Analyzes sessions for cost, prompting patterns, decisions,
topic clusters, and actionable insights. All heuristic-based,
no LLM calls needed.
"""

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple

from ..data.session_parser import SessionParser
from ..data.models import SessionMetadata, ConversationMessage


# ===== Pricing (per 1M tokens, USD) =====
MODEL_PRICING = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
    # Fallback for older/unknown models
    "default": {"input": 15.0, "output": 75.0},
}


def _get_pricing(model: Optional[str]) -> dict:
    if not model:
        return MODEL_PRICING["default"]
    for key, pricing in MODEL_PRICING.items():
        if key in (model or ""):
            return pricing
    return MODEL_PRICING["default"]


# ===== Cost Calculation =====

def calculate_session_cost(session: SessionMetadata) -> dict:
    """Calculate cost for a single session."""
    pricing = _get_pricing(session.model_used)
    input_cost = (session.total_input_tokens / 1_000_000) * pricing["input"]
    output_cost = (session.total_output_tokens / 1_000_000) * pricing["output"]
    total = input_cost + output_cost
    return {
        "input_cost": round(input_cost, 3),
        "output_cost": round(output_cost, 3),
        "total_cost": round(total, 3),
        "input_tokens": session.total_input_tokens,
        "output_tokens": session.total_output_tokens,
        "model": session.model_used,
    }


def calculate_period_costs(sessions: List[SessionMetadata], days: int = 7) -> dict:
    """Calculate costs over a time period."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    period_sessions = [
        s for s in sessions
        if s.start_time.replace(tzinfo=timezone.utc) > cutoff
    ]

    total_cost = 0
    daily_costs = defaultdict(float)
    session_costs = []

    for s in period_sessions:
        cost = calculate_session_cost(s)
        total_cost += cost["total_cost"]
        day_key = s.start_time.strftime("%Y-%m-%d")
        daily_costs[day_key] += cost["total_cost"]
        session_costs.append({
            "session_id": s.session_id,
            "title": s.title,
            "cost": cost["total_cost"],
            "tokens": cost["input_tokens"] + cost["output_tokens"],
        })

    session_costs.sort(key=lambda x: -x["cost"])

    return {
        "total_cost": round(total_cost, 2),
        "session_count": len(period_sessions),
        "daily_costs": dict(sorted(daily_costs.items())),
        "avg_per_session": round(total_cost / len(period_sessions), 2) if period_sessions else 0,
        "most_expensive": session_costs[:5],
        "days": days,
    }


# ===== Prompt Efficiency Analysis =====

def analyze_prompt_efficiency(session_id: str, parser: SessionParser) -> dict:
    """Analyze a single session for prompting efficiency."""
    messages = parser.get_session_messages(session_id, limit=2000)
    if not messages:
        return {}

    user_msgs = [m for m in messages if m.type == "user"]
    asst_msgs = [m for m in messages if m.type == "assistant"]

    # Metrics
    user_count = len(user_msgs)
    asst_count = len(asst_msgs)
    total_user_chars = sum(len(m.content) for m in user_msgs)
    total_asst_chars = sum(len(m.content) for m in asst_msgs)

    # Pasted content analysis
    pasted_chars = 0
    for m in user_msgs:
        if len(m.content) > 1000:
            # Rough heuristic: content > 1000 chars is likely pasted
            pasted_chars += len(m.content)

    paste_ratio = pasted_chars / total_user_chars if total_user_chars else 0

    # Back-and-forth ratio
    clarification_count = 0
    for i, m in enumerate(user_msgs[1:], 1):
        content_lower = m.content.lower().strip()
        # Short follow-ups that suggest clarification
        if len(content_lower) < 100 and any(p in content_lower for p in [
            "no", "not that", "i mean", "what about", "yes", "correct",
            "actually", "wait", "sorry", "can you", "try again"
        ]):
            clarification_count += 1

    # First message quality
    first_msg = user_msgs[0].content if user_msgs else ""
    first_msg_len = len(first_msg)
    first_msg_is_vague = first_msg_len < 50 and not any(
        c in first_msg for c in ["```", "|", "http", "/"]
    )

    # Tool call density (sessions with many tool calls = Claude doing work)
    tool_calls = sum(len(m.tool_details) for m in asst_msgs)
    tool_heavy = tool_calls > 10

    insights = []

    # Generate insights
    if paste_ratio > 0.5:
        saved = int(pasted_chars * 0.7)  # Could save ~70% by summarizing
        insights.append({
            "type": "cost",
            "severity": "high",
            "message": f"{int(paste_ratio * 100)}% of your input was pasted content ({pasted_chars:,} chars). Summarizing before pasting could save ~{saved:,} input tokens.",
            "tip": "Before pasting large text, add a one-line instruction like 'Here's a job description. Tell me if the role is worth pursuing given [your context].' This focuses Claude's response.",
        })

    if clarification_count >= 3:
        insights.append({
            "type": "efficiency",
            "severity": "medium",
            "message": f"You had {clarification_count} clarification exchanges. A more specific first prompt would have saved {clarification_count * 2} messages.",
            "tip": "Structure your request as: [Context] + [Specific ask] + [Constraints/format]. Example: 'I'm a Director of PM at ServiceNow (context). Should I interview at Adobe for this role (ask)? Compare comp, career trajectory, and market position (constraints).'",
        })

    if first_msg_is_vague:
        insights.append({
            "type": "efficiency",
            "severity": "low",
            "message": f"Your opening message was {first_msg_len} chars — likely too brief for Claude to understand your full intent.",
            "tip": "The first message sets the trajectory. Spending 30 seconds writing a detailed first prompt saves minutes of back-and-forth.",
        })

    if user_count > 0 and asst_count > 0:
        output_ratio = total_asst_chars / total_user_chars if total_user_chars else 0
        if output_ratio > 20:
            insights.append({
                "type": "positive",
                "severity": "info",
                "message": f"High leverage session — Claude produced {int(output_ratio)}x more content than you input. This is an efficient use pattern.",
            })

    if tool_heavy and user_count <= 3:
        insights.append({
            "type": "positive",
            "severity": "info",
            "message": f"Great delegation — {tool_calls} tool calls from just {user_count} prompts. Claude did heavy lifting autonomously.",
        })

    return {
        "user_messages": user_count,
        "assistant_messages": asst_count,
        "total_user_chars": total_user_chars,
        "total_assistant_chars": total_asst_chars,
        "paste_ratio": round(paste_ratio, 2),
        "clarification_count": clarification_count,
        "tool_calls": tool_calls,
        "first_msg_length": first_msg_len,
        "insights": insights,
    }


# ===== Cross-Session Patterns =====

def analyze_cross_session_patterns(sessions: List[SessionMetadata], parser: SessionParser) -> dict:
    """Analyze patterns across multiple sessions."""
    if not sessions:
        return {}

    # Cost overview
    costs = calculate_period_costs(sessions, days=9999)

    # Sessions by day of week
    day_counts = Counter()
    hour_counts = Counter()
    for s in sessions:
        day_counts[s.start_time.strftime("%A")] += 1
        hour_counts[s.start_time.hour] += 1

    peak_day = day_counts.most_common(1)[0] if day_counts else ("Unknown", 0)
    peak_hour = hour_counts.most_common(1)[0] if hour_counts else (0, 0)

    # Model usage
    model_counts = Counter(s.model_used or "unknown" for s in sessions)

    # Session length distribution
    short = sum(1 for s in sessions if s.message_count < 10)
    medium = sum(1 for s in sessions if 10 <= s.message_count < 50)
    long_sessions = sum(1 for s in sessions if s.message_count >= 50)

    # Pasted content frequency
    paste_sessions = sum(1 for s in sessions if s.has_pasted_content)

    insights = []

    if paste_sessions > len(sessions) * 0.3:
        insights.append({
            "type": "pattern",
            "message": f"{int(paste_sessions/len(sessions)*100)}% of your sessions include pasted external content. Consider creating reusable context files for recurring topics.",
        })

    if short > len(sessions) * 0.4:
        insights.append({
            "type": "pattern",
            "message": f"{int(short/len(sessions)*100)}% of sessions are under 10 messages. Many quick questions could be batched into fewer, richer sessions.",
        })

    return {
        "total_sessions": len(sessions),
        "total_cost": costs["total_cost"],
        "avg_cost_per_session": costs["avg_per_session"],
        "peak_day": peak_day[0],
        "peak_hour": peak_hour[0],
        "model_usage": dict(model_counts.most_common()),
        "session_length": {"short": short, "medium": medium, "long": long_sessions},
        "paste_frequency": paste_sessions,
        "most_expensive": costs["most_expensive"][:3],
        "insights": insights,
    }


# ===== Decision Extraction =====

DECISION_PATTERNS = [
    (r"(?:my|the)\s+(?:recommendation|take|advice|verdict)\s*(?:is|:)", "recommendation"),
    (r"(?:skip|pass on|don't|do not)\s+(?:this|it|that)", "rejection"),
    (r"(?:go with|choose|pick|take|accept)\s+", "selection"),
    (r"(?:you should|i(?:'d| would))\s+(?:stay|leave|move|switch|focus)", "direction"),
    (r"(?:the answer|conclusion|bottom line|net-net)\s*(?:is|:)", "conclusion"),
    (r"(?:decision|decided|deciding)\s*(?:to|:)", "decision"),
]


def extract_decisions(session_id: str, parser: SessionParser) -> List[dict]:
    """Extract decision-like statements from a session."""
    messages = parser.get_session_messages(session_id, limit=2000)
    decisions = []

    for msg in messages:
        if msg.type != "assistant" or not msg.content:
            continue

        content = msg.content
        for pattern, decision_type in DECISION_PATTERNS:
            matches = list(re.finditer(pattern, content, re.IGNORECASE))
            for match in matches:
                # Extract surrounding context (the sentence containing the match)
                start = max(0, content.rfind(".", 0, match.start()) + 1)
                end = content.find(".", match.end())
                if end == -1:
                    end = min(len(content), match.end() + 200)
                else:
                    end += 1

                snippet = content[start:end].strip()
                if len(snippet) > 30:  # Skip tiny fragments
                    decisions.append({
                        "type": decision_type,
                        "text": snippet[:300],
                        "timestamp": msg.timestamp.isoformat(),
                        "session_id": session_id,
                    })

    # Deduplicate similar decisions
    seen = set()
    unique = []
    for d in decisions:
        key = d["text"][:50].lower()
        if key not in seen:
            seen.add(key)
            unique.append(d)

    return unique[:10]  # Cap at 10 per session


# ===== Topic Extraction (TF-IDF lite) =====

STOP_WORDS = set("the a an is are was were be been being have has had do does did will would shall should may might can could i you he she it we they me him her us them my your his its our their this that these those am in on at to for of with by from as into through during before after above below between".split())


def extract_topics(session_id: str, parser: SessionParser, top_n: int = 8) -> List[str]:
    """Extract key topics from a session using word frequency."""
    messages = parser.get_session_messages(session_id, limit=500)
    words = Counter()

    for msg in messages:
        if not msg.content:
            continue
        # Simple tokenization
        tokens = re.findall(r"[a-zA-Z]{3,}", msg.content.lower())
        for t in tokens:
            if t not in STOP_WORDS and len(t) > 3:
                words[t] += 1

    # Return most common meaningful words
    return [w for w, _ in words.most_common(top_n * 2) if len(w) > 4][:top_n]


def find_related_sessions(
    session_id: str,
    all_sessions: List[SessionMetadata],
    parser: SessionParser,
    top_n: int = 5,
) -> List[dict]:
    """Find sessions related to the given one by topic overlap."""
    target_topics = set(extract_topics(session_id, parser, top_n=15))
    if not target_topics:
        return []

    scores = []
    for s in all_sessions:
        if s.session_id == session_id:
            continue
        # Use title + first message for quick comparison
        text = (s.title or "") + " " + (s.first_user_message or "")
        tokens = set(re.findall(r"[a-zA-Z]{4,}", text.lower()))
        overlap = len(target_topics & tokens)
        if overlap >= 2:
            scores.append({
                "session_id": s.session_id,
                "title": s.title,
                "overlap": overlap,
                "shared_topics": list(target_topics & tokens)[:5],
                "last_activity": s.last_activity.isoformat() if s.last_activity else None,
            })

    scores.sort(key=lambda x: -x["overlap"])
    return scores[:top_n]


# ===== Prompting Playbook Generator =====

def generate_prompting_playbook(
    sessions: List[SessionMetadata], parser: SessionParser,
    days: int = 9999, compare_previous: bool = True,
) -> dict:
    """Analyze sessions and generate a personalized prompting playbook.

    Returns actionable recommendations based on actual usage patterns,
    plus a CLAUDE.md-compatible instruction block.

    When days < 9999, only analyzes sessions within that time window
    and optionally compares to the previous equivalent period for trends.
    """
    if not sessions:
        return {"recommendations": [], "claude_md_block": "", "score": 0, "trends": {}}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days) if days < 9999 else None

    # Filter sessions to the selected time window
    if cutoff:
        period_sessions = [
            s for s in sessions
            if s.start_time.replace(tzinfo=timezone.utc) > cutoff
        ]
    else:
        period_sessions = sessions

    if not period_sessions:
        return {"recommendations": [], "claude_md_block": "", "score": 0, "trends": {}}

    # Analyze a sample — up to 30 sessions (more than before for better signal)
    sample = period_sessions[:30]
    all_efficiencies = []
    for s in sample:
        try:
            eff = analyze_prompt_efficiency(s.session_id, parser)
            if eff:
                eff["session_id"] = s.session_id
                eff["title"] = s.title
                all_efficiencies.append(eff)
        except Exception:
            continue

    if not all_efficiencies:
        return {"recommendations": [], "claude_md_block": "", "score": 0}

    # Aggregate metrics
    avg_clarifications = sum(e.get("clarification_count", 0) for e in all_efficiencies) / len(all_efficiencies)
    avg_paste_ratio = sum(e.get("paste_ratio", 0) for e in all_efficiencies) / len(all_efficiencies)
    avg_first_msg_len = sum(e.get("first_msg_length", 0) for e in all_efficiencies) / len(all_efficiencies)
    total_tool_calls = sum(e.get("tool_calls", 0) for e in all_efficiencies)
    high_leverage = sum(1 for e in all_efficiencies if e.get("total_assistant_chars", 0) > e.get("total_user_chars", 1) * 10)

    # Score (0-100)
    score = 70  # baseline
    if avg_clarifications > 3:
        score -= 15
    elif avg_clarifications < 1:
        score += 10
    if avg_paste_ratio > 0.5:
        score -= 10
    if avg_first_msg_len > 200:
        score += 10
    elif avg_first_msg_len < 50:
        score -= 10
    if high_leverage > len(all_efficiencies) * 0.5:
        score += 10
    score = max(20, min(95, score))

    # Collect real session examples for recommendations
    vague_openers = []  # sessions with short/vague first messages
    heavy_paste_sessions = []  # sessions with high paste ratio
    clarification_heavy = []  # sessions with many clarifications
    high_leverage_sessions = []  # sessions with good delegation
    tool_heavy_sessions = []  # sessions with many tool calls

    for eff in all_efficiencies:
        sid = eff.get("session_id", "")
        title = eff.get("title", sid[:8])
        fml = eff.get("first_msg_length", 0)
        first_msg = ""
        # Get actual first message text
        try:
            msgs = parser.get_session_messages(sid, limit=5)
            user_msgs = [m for m in msgs if m.type == "user"]
            if user_msgs:
                first_msg = user_msgs[0].content.strip()[:200]
        except Exception:
            pass

        if fml < 80 and first_msg:
            vague_openers.append({"session_id": sid, "title": title, "first_msg": first_msg, "length": fml})
        if eff.get("paste_ratio", 0) > 0.4:
            heavy_paste_sessions.append({"session_id": sid, "title": title, "paste_ratio": eff["paste_ratio"], "pasted_chars": int(eff.get("total_user_chars", 0) * eff["paste_ratio"])})
        if eff.get("clarification_count", 0) >= 3:
            clarification_heavy.append({"session_id": sid, "title": title, "count": eff["clarification_count"]})
        if eff.get("total_assistant_chars", 0) > eff.get("total_user_chars", 1) * 10:
            high_leverage_sessions.append({"session_id": sid, "title": title, "ratio": round(eff.get("total_assistant_chars", 0) / max(1, eff.get("total_user_chars", 1)))})
        if eff.get("tool_calls", 0) > 10:
            tool_heavy_sessions.append({"session_id": sid, "title": title, "tool_calls": eff["tool_calls"]})

    # Generate recommendations with real examples
    recommendations = []

    # 1. First message quality
    if avg_first_msg_len < 80 and vague_openers:
        examples = vague_openers[:3]
        example_lines = []
        for ex in examples:
            example_lines.append(f'"{ex["first_msg"][:100]}{"..." if len(ex["first_msg"]) > 100 else ""}" ({ex["length"]} chars) — {ex["title"]}')
        recommendations.append({
            "title": "Write richer opening prompts",
            "description": f"Your average first message is {int(avg_first_msg_len)} characters. {len(vague_openers)} of {len(all_efficiencies)} sessions started with a vague opener.",
            "action": "Structure as: [Your role/context] + [Specific question] + [Desired format/constraints]",
            "sessions": [{"session_id": ex["session_id"], "title": ex["title"], "detail": f'"{ex["first_msg"][:100]}..." ({ex["length"]} chars)'} for ex in examples],
            "impact": "high",
        })
    elif avg_first_msg_len >= 80:
        recommendations.append({
            "title": "Your opening prompts are strong",
            "description": f"Average first message is {int(avg_first_msg_len)} chars — you give Claude enough context to work with.",
            "impact": "positive",
        })

    # 2. Pasted content
    if avg_paste_ratio > 0.3 and heavy_paste_sessions:
        heavy_paste_sessions.sort(key=lambda x: -x["pasted_chars"])
        examples = heavy_paste_sessions[:3]
        recommendations.append({
            "title": "Summarize before pasting",
            "description": f"{int(avg_paste_ratio * 100)}% of your input is pasted external content across {len(heavy_paste_sessions)} sessions.",
            "action": "Before pasting, add a 1-line instruction. For large content, paste only the relevant section.",
            "sessions": [{"session_id": ex["session_id"], "title": ex["title"], "detail": f'{int(ex["paste_ratio"]*100)}% pasted ({ex["pasted_chars"]:,} chars)'} for ex in examples],
            "impact": "high",
        })

    # 3. Clarification loops
    if avg_clarifications > 2 and clarification_heavy:
        clarification_heavy.sort(key=lambda x: -x["count"])
        examples = clarification_heavy[:3]
        recommendations.append({
            "title": "Reduce back-and-forth clarifications",
            "description": f"You average {avg_clarifications:.1f} clarification exchanges per session. {len(clarification_heavy)} sessions had 3+ rounds of back-and-forth.",
            "action": "Anticipate what Claude will need to know. Include constraints, examples of desired output, and what NOT to do.",
            "sessions": [{"session_id": ex["session_id"], "title": ex["title"], "detail": f'{ex["count"]} clarification rounds'} for ex in examples],
            "impact": "medium",
        })

    # 4. Batching
    short_sessions_list = [s for s in period_sessions if s.message_count < 5]
    if len(short_sessions_list) > len(period_sessions) * 0.3:
        examples = short_sessions_list[:3]
        recommendations.append({
            "title": "Batch related quick questions",
            "description": f"{len(short_sessions_list)} of {len(period_sessions)} sessions ({int(len(short_sessions_list)/len(period_sessions)*100)}%) are very short (< 5 messages).",
            "action": "Group related questions into one session. Use numbered lists for multiple questions.",
            "sessions": [{"session_id": s.session_id, "title": s.title or s.session_id[:8], "detail": f'{s.message_count} messages'} for s in examples],
            "impact": "medium",
        })

    # 5. Leverage patterns
    if high_leverage_sessions:
        high_leverage_sessions.sort(key=lambda x: -x["ratio"])
        examples = high_leverage_sessions[:3]
        recommendations.append({
            "title": "Good delegation pattern",
            "description": f"{len(high_leverage_sessions)} of {len(all_efficiencies)} sessions show high leverage — Claude produces 10x+ more output than your input.",
            "sessions": [{"session_id": ex["session_id"], "title": ex["title"], "detail": f'{ex["ratio"]}x output ratio'} for ex in examples],
            "impact": "positive",
        })

    # 6. Skills and tools
    if total_tool_calls > 50 and tool_heavy_sessions:
        tool_heavy_sessions.sort(key=lambda x: -x["tool_calls"])
        examples = tool_heavy_sessions[:3]
        recommendations.append({
            "title": "Power tool user",
            "description": f"{total_tool_calls} tool calls across {len(all_efficiencies)} sessions. {len(tool_heavy_sessions)} sessions were tool-heavy.",
            "action": "Consider creating /skills for recurring tool-heavy workflows to reduce prompting overhead.",
            "sessions": [{"session_id": ex["session_id"], "title": ex["title"], "detail": f'{ex["tool_calls"]} tool calls'} for ex in examples],
            "impact": "medium",
        })

    # Generate CLAUDE.md block
    claude_md_lines = [
        "# Prompting Guidelines (auto-generated from usage patterns)",
        "",
    ]
    if avg_first_msg_len < 80:
        claude_md_lines.append("- When my first message is vague or under 80 characters, ask me to clarify my role, specific question, and desired output format before proceeding.")
    if avg_paste_ratio > 0.3:
        claude_md_lines.append("- When I paste large blocks of text, focus only on the parts relevant to my question. Summarize the pasted content before analyzing.")
    if avg_clarifications > 2:
        claude_md_lines.append("- Before giving a long response, confirm you understand my intent in 1 sentence. This reduces back-and-forth.")
    claude_md_lines.extend([
        "- Always lead with the actionable answer, then supporting detail. I don't need preamble.",
        "- When I ask for comparison or evaluation, use tables and give a clear recommendation — don't just list pros/cons.",
        "- Reference my previous sessions when relevant (I use /pin and /recall for context continuity).",
    ])
    claude_md_block = "\n".join(claude_md_lines)

    # Compute trends by comparing to previous period
    trends = {}
    if compare_previous and cutoff and days < 9999:
        prev_cutoff = cutoff - timedelta(days=days)
        prev_sessions = [
            s for s in sessions
            if prev_cutoff < s.start_time.replace(tzinfo=timezone.utc) <= cutoff
        ]
        if prev_sessions:
            prev_sample = prev_sessions[:30]
            prev_efficiencies = []
            for s in prev_sample:
                try:
                    eff = analyze_prompt_efficiency(s.session_id, parser)
                    if eff:
                        prev_efficiencies.append(eff)
                except Exception:
                    continue

            if prev_efficiencies:
                prev_avg_clar = sum(e.get("clarification_count", 0) for e in prev_efficiencies) / len(prev_efficiencies)
                prev_avg_paste = sum(e.get("paste_ratio", 0) for e in prev_efficiencies) / len(prev_efficiencies)
                prev_avg_fml = sum(e.get("first_msg_length", 0) for e in prev_efficiencies) / len(prev_efficiencies)
                prev_high_lev = sum(1 for e in prev_efficiencies if e.get("total_assistant_chars", 0) > e.get("total_user_chars", 1) * 10)

                # Compute previous score
                prev_score = 70
                if prev_avg_clar > 3: prev_score -= 15
                elif prev_avg_clar < 1: prev_score += 10
                if prev_avg_paste > 0.5: prev_score -= 10
                if prev_avg_fml > 200: prev_score += 10
                elif prev_avg_fml < 50: prev_score -= 10
                if prev_high_lev > len(prev_efficiencies) * 0.5: prev_score += 10
                prev_score = max(20, min(95, prev_score))

                trends = {
                    "score_change": score - prev_score,
                    "clarification_change": round(avg_clarifications - prev_avg_clar, 1),
                    "paste_ratio_change": round((avg_paste_ratio - prev_avg_paste) * 100),
                    "first_msg_change": int(avg_first_msg_len - prev_avg_fml),
                    "prev_sessions_analyzed": len(prev_efficiencies),
                    "prev_score": prev_score,
                }

    import time as _time
    return {
        "recommendations": recommendations,
        "claude_md_block": claude_md_block,
        "score": score,
        "metrics": {
            "avg_clarifications": round(avg_clarifications, 1),
            "avg_paste_ratio": round(avg_paste_ratio * 100),
            "avg_first_msg_length": int(avg_first_msg_len),
            "high_leverage_pct": int(high_leverage / len(all_efficiencies) * 100) if all_efficiencies else 0,
            "sessions_analyzed": len(all_efficiencies),
        },
        "trends": trends,
        "generated_at": _time.time(),
        "period_days": days,
    }
