#!/usr/bin/env python3
"""Daily cron job: archive Claude sessions before they expire.

Archives sessions older than 25 days (before Claude Code's ~30 day cleanup).
Runs independently of the dashboard. Safe to run multiple times.

Usage:
    python3 archive_cron.py          # archive expiring sessions
    python3 archive_cron.py --all    # archive everything (first run)
"""

import sys
import time
from pathlib import Path

# Add parent to path so imports work
sys.path.insert(0, str(Path(__file__).parent))

from claude_sessions.data.archive import SessionArchive
from claude_sessions.data.session_parser import SessionParser


def main():
    archive = SessionArchive()
    parser = SessionParser()

    archive_all = "--all" in sys.argv
    cutoff_days = 0 if archive_all else 25
    cutoff = time.time() - (cutoff_days * 86400)

    sessions = parser.get_all_sessions()
    archived = 0
    skipped = 0

    for session in sessions:
        if archive_all or session.start_time.timestamp() < cutoff:
            if archive.archive_session(session.session_id):
                archived += 1
                print(f"  Archived: {session.title or session.session_id[:8]}")
            else:
                skipped += 1

    # Mark gone sessions
    gone = archive._mark_gone_sessions()

    stats = archive.get_stats()
    print(f"\nDone. Archived: {archived}, Skipped (already saved): {skipped}, Gone: {gone}")
    print(f"Total in archive: {stats['total']} sessions, {stats['total_messages']} messages")

    # Auto-generate descriptions for sessions missing them
    from claude_sessions.services.session_describer import describe_session, get_cached_description
    described = 0
    for session in sessions:
        if not get_cached_description(session.session_id):
            try:
                describe_session(session.session_id, parser)
                described += 1
                print(f"  Described: {session.title or session.session_id[:8]}")
            except Exception as e:
                print(f"  Describe failed for {session.session_id[:8]}: {e}")
    if described:
        print(f"Generated {described} new descriptions")

    # Generate journal entry (Marginalia)
    try:
        from claude_sessions.services.journal_writer import run_journal_cron
        run_journal_cron(parser)
    except Exception as e:
        print(f"Journal generation failed: {e}")

    # Extract dimensional observations
    try:
        from claude_sessions.services.dimension_extractor import extract_daily_observations
        from datetime import datetime, timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        result = extract_daily_observations(yesterday, parser)
        print(f"Dimensions: {result.get('observations_saved', 0)} observations extracted")
    except Exception as e:
        print(f"Dimension extraction failed: {e}")

    # Consolidate dimensions (biweekly)
    try:
        from claude_sessions.services.dimension_consolidator import (
            should_consolidate_dimensions, consolidate_all_dimensions,
        )
        if should_consolidate_dimensions():
            print("  Running dimension consolidation...")
            result = consolidate_all_dimensions()
            print(f"  Dimensions consolidated: {len(result.get('updated', []))} profiles")

            from claude_sessions.services.profile_exporter import apply_profile_to_claude_md
            apply_profile_to_claude_md()
    except Exception as e:
        print(f"Dimension consolidation failed: {e}")

    # Update semantic index if stale
    try:
        from claude_sessions.data.semantic_index import SemanticIndex
        sem = SemanticIndex()
        if sem.is_stale():
            result = sem.build_index()
            print(f"Semantic index: {result.get('chunks_indexed', 0)} new chunks indexed")
    except Exception as e:
        print(f"Semantic index update failed: {e}")


if __name__ == "__main__":
    main()
