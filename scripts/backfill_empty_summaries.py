#!/usr/bin/env python3
"""
backfill_empty_summaries.py — Targeted backfill for the 2026-05-27/28 Ollama-wedge incident.

CONTEXT
-------
Between 2026-05-27 ~16:50 PDT and 2026-05-28 09:34:58 PDT, the contextgraph
daemon's Ollama summarization path was wedged on stale HTTP connection state
(see ~/.sybilclaw/workspace-jarvis/projects/contextgraph-audit-2026-05-28/
TECHNICAL-REPORT.md for the full root-cause). Every /ingest in that window:

  - HTTP 200 returned to the caller
  - Row landed in ~/.tag-context/store.db
  - But the summary field landed empty (Ollama timed out, fallback fired)

Rows in that window that exceeded SUMMARIZE_THRESHOLD lost their summaries.
This script finds those rows, regenerates summaries via the (now-fixed)
summarizer, and writes them back.

USAGE
-----
    # Dry-run (default) — counts and previews work, makes NO writes
    python scripts/backfill_empty_summaries.py

    # Commit — actually call the summarizer and write summaries back
    python scripts/backfill_empty_summaries.py --commit

    # Override window bounds (defaults match the 2026-05-27/28 incident)
    python scripts/backfill_empty_summaries.py --commit \
        --since 2026-05-27T16:50 --until 2026-05-28T09:34:58

    # Override token threshold (default: $SUMMARIZE_THRESHOLD or 2000)
    python scripts/backfill_empty_summaries.py --commit --threshold 1500

BEHAVIOR
--------
- Selects rows where summary IS NULL OR summary = '' AND token_count > threshold
  AND timestamp falls within [--since, --until].
- Batches in groups of --batch (default 10) with --sleep seconds between batches.
- Prints one line per row: status, id, token_count, summary length.
- Per-batch progress line at the end.
- Reports final tally.

EXIT CODES
----------
0 — success (dry-run or commit). 1 — usage error. 2 — store unreachable.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add parent directory to import project modules
sys.path.insert(0, str(Path(__file__).parent.parent))


def parse_local_or_iso(s: str) -> float:
    """Parse a wall-clock string into a Unix timestamp.

    Accepts:
      - ISO-8601 with timezone (e.g. '2026-05-27T16:50:00-07:00')
      - ISO-8601 without timezone (assumed America/Los_Angeles wall time)
      - Date-only 'YYYY-MM-DD' (assumed local midnight)
    """
    # Common shapes — let datetime.fromisoformat handle most
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"can't parse {s!r}: {e}")
    if dt.tzinfo is None:
        # America/Los_Angeles is UTC-7 during PDT (2026-05-27/28). Use a
        # fixed offset rather than zoneinfo to avoid the tzdata dependency
        # and to make the conversion deterministic.
        dt = dt.replace(tzinfo=timezone(timedelta(hours=-7)))
    return dt.timestamp()


def main() -> int:
    # Default window matches the 2026-05-27/28 wedge.
    default_since = "2026-05-27T16:50:00"
    default_until = "2026-05-28T09:34:58"

    parser = argparse.ArgumentParser(
        description="Backfill empty summaries from the 2026-05-27/28 Ollama-wedge window",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write summaries. Default is dry-run (no writes).",
    )
    parser.add_argument(
        "--since",
        type=parse_local_or_iso,
        default=parse_local_or_iso(default_since),
        help=f"Lower bound timestamp. Default: {default_since} (PDT).",
    )
    parser.add_argument(
        "--until",
        type=parse_local_or_iso,
        default=parse_local_or_iso(default_until),
        help=f"Upper bound timestamp. Default: {default_until} (PDT).",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="Token-count threshold; rows with token_count > threshold are candidates. "
        "Default: $SUMMARIZE_THRESHOLD or 2000.",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=10,
        help="Rows per batch (with sleep between batches). Default 10.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Seconds to sleep between batches. Default 1.0.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Hard cap on rows processed. Default: no cap.",
    )
    args = parser.parse_args()

    # Defer imports so --help works without the env
    try:
        from store import MessageStore
        from summarizer import summarize_message, SUMMARIZER_BACKEND, SUMMARIZER_MODEL
    except ImportError as e:
        print(f"FATAL: can't import contextgraph modules: {e}", file=sys.stderr)
        return 2

    import os
    threshold = args.threshold
    if threshold is None:
        threshold = int(os.getenv("SUMMARIZE_THRESHOLD", "2000"))

    print(f"== backfill_empty_summaries ==")
    print(f"window: {datetime.fromtimestamp(args.since).isoformat()} → {datetime.fromtimestamp(args.until).isoformat()}")
    print(f"        ({args.since:.0f} → {args.until:.0f})")
    print(f"threshold: token_count > {threshold}")
    print(f"backend: {SUMMARIZER_BACKEND} / model: {SUMMARIZER_MODEL}")
    print(f"mode: {'COMMIT (will write)' if args.commit else 'DRY-RUN (no writes)'}")
    print()

    try:
        store = MessageStore()
    except Exception as e:
        print(f"FATAL: can't open MessageStore: {e}", file=sys.stderr)
        return 2

    conn = store._conn()
    query = """
        SELECT id, token_count, timestamp, length(COALESCE(summary,'')) AS sum_len,
               substr(user_text, 1, 60) AS uhead
        FROM messages
        WHERE token_count > ?
          AND (summary IS NULL OR summary = '')
          AND timestamp BETWEEN ? AND ?
        ORDER BY timestamp ASC
    """
    if args.limit:
        query += f" LIMIT {int(args.limit)}"
    rows = conn.execute(query, (threshold, args.since, args.until)).fetchall()
    total = len(rows)
    print(f"Found {total} candidate row(s).")
    if total == 0:
        print()
        print("Nothing to do. Window/threshold combo produces zero matches.")
        return 0

    if not args.commit:
        print()
        print("DRY-RUN — would summarize the following rows:")
        for r in rows[:50]:
            ts = datetime.fromtimestamp(r["timestamp"]).isoformat(timespec="seconds")
            print(
                f"  id={r['id']:<40s}  ts={ts}  tokens={r['token_count']:>5d}  "
                f"summary_len={r['sum_len']:>4d}  user={r['uhead']!r}"
            )
        if total > 50:
            print(f"  ... and {total - 50} more.")
        return 0

    # Commit path
    ok = 0
    fail = 0
    for i, r in enumerate(rows, start=1):
        msg = store.get_by_id(r["id"])
        if msg is None:
            print(f"[{i}/{total}] SKIP id={r['id']} (row disappeared)")
            fail += 1
        else:
            try:
                summary = summarize_message(msg)
                if summary and summary.strip():
                    store.set_summary(msg.id, summary.strip())
                    print(
                        f"[{i}/{total}] OK   id={msg.id} tokens={msg.token_count} "
                        f"summary_len={len(summary)}"
                    )
                    ok += 1
                else:
                    print(
                        f"[{i}/{total}] EMPTY id={msg.id} tokens={msg.token_count} "
                        f"(summarizer returned empty; row left untouched)"
                    )
                    fail += 1
            except Exception as e:
                print(f"[{i}/{total}] FAIL id={msg.id}: {e}")
                fail += 1

        # Batch sleep
        if i % args.batch == 0 and i < total:
            print(f"  ...batch boundary, sleeping {args.sleep}s")
            time.sleep(args.sleep)

    print()
    print(f"== done ==  ok={ok}  fail={fail}  total={total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
