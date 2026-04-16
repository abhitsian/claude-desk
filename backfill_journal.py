#!/usr/bin/env python3
"""Backfill journal entries for the past N days."""

import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from claude_sessions.data.session_parser import SessionParser
from claude_sessions.data.archive import SessionArchive
from claude_sessions.data.journal import get_journal_entry
from claude_sessions.services.journal_writer import generate_journal_entry

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30

parser = SessionParser()
archive = SessionArchive()

# Find days with sessions
live = parser.get_all_sessions()
archived = archive.get_archived_only(limit=1000)

days_with_sessions = defaultdict(int)
for s in live:
    days_with_sessions[s.start_time.strftime('%Y-%m-%d')] += 1

seen = {s.session_id for s in live}
for a in archived:
    if a['session_id'] not in seen:
        days_with_sessions[datetime.fromisoformat(a['start_time']).strftime('%Y-%m-%d')] += 1

today = datetime.now().date()
dates_to_process = []
for i in range(DAYS):
    d = (today - timedelta(days=i)).isoformat()
    if days_with_sessions.get(d, 0) > 0:
        dates_to_process.append(d)

print(f"Backfilling {len(dates_to_process)} days (skipping {DAYS - len(dates_to_process)} empty days)")
print(f"Estimated time: ~{len(dates_to_process) * 30 // 60} minutes\n")

success = 0
skipped = 0
failed = 0
start_all = time.time()

for idx, date in enumerate(dates_to_process):
    sessions = days_with_sessions[date]
    existing = get_journal_entry(date)

    if existing:
        print(f"  [{idx+1}/{len(dates_to_process)}] {date} ({sessions} sessions) — already exists, skipping")
        skipped += 1
        continue

    print(f"  [{idx+1}/{len(dates_to_process)}] {date} ({sessions} sessions) — generating...", end=" ", flush=True)
    start = time.time()

    try:
        result = generate_journal_entry(date, parser)
        elapsed = time.time() - start

        if result and isinstance(result, dict):
            entry = result.get('entry', result)
            tone = entry.get('emotional_tone', '?') if isinstance(entry, dict) else '?'
            orb_count = len(result.get('orbs', []))
            print(f"done ({elapsed:.0f}s) — {tone}, {orb_count} orbs")
            success += 1
        else:
            print(f"no result ({elapsed:.0f}s)")
            failed += 1
    except Exception as e:
        elapsed = time.time() - start
        print(f"error ({elapsed:.0f}s): {e}")
        failed += 1

total_time = time.time() - start_all
print(f"\nDone in {total_time:.0f}s ({total_time/60:.1f} min)")
print(f"  Written: {success}  |  Skipped: {skipped}  |  Failed: {failed}")
