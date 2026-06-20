"""Dimension consolidation — periodic synthesis of observations into profiles.

Like autoDream for the journal, but per-dimension. Re-reads observations,
detects trajectory changes, rewrites the profile.
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from ..data.dimensions import (
    get_observations_since_last_consolidation, get_profile, get_all_profiles,
    get_history, upsert_profile, save_history_snapshot, VALID_DIMENSIONS,
)
from .llm import call_claude, parse_json_response


CONSOLIDATION_PROMPT = """You are consolidating observations about a person's {dimension_label} patterns.

Current profile (what you believed before):
{current_profile}

New observations since last consolidation:
{observations}

Previous profile snapshots (for trajectory detection):
{history}

Rewrite the profile based on all evidence. Respond with JSON:
{{
  "profile": {{
    "current_state": "2-3 sentences describing the current state of this dimension",
    "key_patterns": ["pattern 1", "pattern 2", "pattern 3"],
    "notable_shifts": ["anything that changed since the last profile"],
    "data_points": {{}}
  }},
  "summary": "1-2 sentence summary for display — specific, not generic",
  "trajectory": "what direction this dimension is moving (e.g., 'getting more precise', 'stable', 'shifting from X to Y')",
  "confidence": 0.7
}}

Rules:
- Be specific. Reference actual patterns from the observations, not generic descriptions.
- If nothing meaningful changed, say so. Don't manufacture trajectory.
- The summary should be tweetable — one line that captures this dimension."""

DIMENSION_LABELS = {
    "taste": "taste and quality judgment",
    "reasoning": "reasoning and problem-solving",
    "energy": "energy and attention",
    "tempo": "tempo and pace",
    "language": "language and vocabulary",
}


def consolidate_dimension(dimension: str) -> Optional[dict]:
    """Consolidate a single dimension's observations into an updated profile."""
    current = get_profile(dimension)
    new_obs = get_observations_since_last_consolidation(dimension)
    history = get_history(dimension, limit=3)

    if not new_obs:
        return None

    # Archive current profile as history before overwriting
    if current:
        save_history_snapshot(
            dimension=dimension,
            profile_snapshot=current["profile"],
            summary=current["summary"],
            observation_count=current["observation_count"],
        )

    current_text = json.dumps(current["profile"], indent=2) if current else "No profile yet — this is the first consolidation."
    obs_text = "\n\n".join(
        f"[{o['date']}] ({o['signal_count']} signals)\n{json.dumps(o['data'], indent=2)}"
        for o in new_obs[:20]
    )
    history_text = "\n\n".join(
        f"[{h['created_at'][:10]}] {h['summary']}"
        for h in history
    ) if history else "No history yet."

    prompt = CONSOLIDATION_PROMPT.format(
        dimension_label=DIMENSION_LABELS.get(dimension, dimension),
        current_profile=current_text,
        observations=obs_text,
        history=history_text,
    )

    result = call_claude(prompt)
    if not result:
        return None

    parsed = parse_json_response(result)
    if not parsed:
        return None

    total_obs = (current["observation_count"] if current else 0) + len(new_obs)

    upsert_profile(
        dimension=dimension,
        profile=parsed.get("profile", {}),
        summary=parsed.get("summary", ""),
        trajectory=parsed.get("trajectory", ""),
        confidence=parsed.get("confidence", 0.5),
        observation_count=total_obs,
    )

    print(f"  Dimension '{dimension}' consolidated: {len(new_obs)} new observations, confidence {parsed.get('confidence', 0.5)}")
    return parsed


def consolidate_all_dimensions() -> dict:
    """Consolidate all dimensions. Returns summary."""
    updated = []
    skipped = []

    for dim in VALID_DIMENSIONS:
        result = consolidate_dimension(dim)
        if result:
            updated.append(dim)
        else:
            skipped.append(dim)

    return {"updated": updated, "skipped": skipped}


def should_consolidate_dimensions() -> bool:
    """Check if it's time for a dimension consolidation."""
    profiles = get_all_profiles()

    if not profiles:
        # First consolidation — run if any observations exist
        for dim in VALID_DIMENSIONS:
            obs = get_observations_since_last_consolidation(dim)
            if len(obs) >= 3:
                return True
        return False

    # Check if 14+ days since last consolidation
    for p in profiles:
        if p["last_consolidated_at"]:
            last = datetime.fromisoformat(p["last_consolidated_at"]).date()
            if (datetime.now(timezone.utc).date() - last).days >= 14:
                return True

    # Or if any dimension has 20+ unconsolidated observations
    for dim in VALID_DIMENSIONS:
        obs = get_observations_since_last_consolidation(dim)
        if len(obs) >= 20:
            return True

    return False
