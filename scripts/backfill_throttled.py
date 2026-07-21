#!/usr/bin/env python3
"""
backfill_throttled.py — gentle, lock-aware background backfill of empty summaries.

Context (2026-07-21): the bulk backfill (backfill_empty_summaries.py --commit)
ran as a sustained extra writer alongside 5 sync crons + the summarizer and
produced a `database is locked` storm (see AAR-contextgraph-2026-07-21). This
wrapper does the same work but paced so it never stacks into that storm:

  - ONE row per iteration, generous inter-row sleep (default 8s) -> low, steady
    write rate that sits well under the sync-burst threshold.
  - Retry-with-backoff on `database is locked` (waits instead of failing).
  - Re-queries the live remaining set each pass, so it's fully resumable and
    picks up only rows that are still empty (won't fight concurrent ingests).
  - Writes summaries via the SAME MessageStore path the daemon uses.
  - Heartbeat progress line every N rows; final summary on exit.

Safe to kill at any time: each row is committed independently.

Usage:
    python scripts/backfill_throttled.py --threshold 400 --sleep 8 \
        --since 2026-04-01T00:00 --until 2026-07-22T00:00
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))


def _ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=400,
                    help="Only summarize rows with token_count > this (words). Default 400.")
    ap.add_argument("--sleep", type=float, default=8.0,
                    help="Seconds between rows. Higher = gentler. Default 8.")
    ap.add_argument("--since", default="2026-04-01T00:00")
    ap.add_argument("--until", default="2026-07-22T00:00")
    ap.add_argument("--heartbeat", type=int, default=25,
                    help="Log a progress line every N successful rows.")
    ap.add_argument("--max-lock-retries", type=int, default=6)
    args = ap.parse_args()

    from store import MessageStore  # noqa
    from summarizer import summarize_message, _fallback_truncation  # noqa

    store = MessageStore()
    since, until = _ts(args.since), _ts(args.until)

    def remaining_count() -> int:
        c = store._conn()
        return c.execute(
            "SELECT COUNT(*) FROM messages "
            "WHERE (summary IS NULL OR summary='') AND token_count > ? "
            "AND timestamp BETWEEN ? AND ?",
            (args.threshold, since, until),
        ).fetchone()[0]

    def next_batch(limit: int = 50):
        c = store._conn()
        return c.execute(
            "SELECT id FROM messages "
            "WHERE (summary IS NULL OR summary='') AND token_count > ? "
            "AND timestamp BETWEEN ? AND ? ORDER BY timestamp ASC LIMIT ?",
            (args.threshold, since, until, limit),
        ).fetchall()

    start_remaining = remaining_count()
    print(f"[throttled-backfill] start: {start_remaining} rows remaining "
          f"(threshold>{args.threshold} words, sleep={args.sleep}s) @ {datetime.now():%H:%M:%S}",
          flush=True)
    if start_remaining == 0:
        print("[throttled-backfill] nothing to do.", flush=True)
        return 0

    ok = fail = 0
    t0 = time.time()
    while True:
        rows = next_batch(50)
        if not rows:
            break
        for (mid,) in rows:
            msg = store.get_by_id(mid)
            if msg is None:
                continue
            # Skip if it got summarized by the daemon in the meantime.
            cur = store.get_summary(mid)
            if cur and cur.strip():
                continue
            try:
                summary = summarize_message(msg)
                if not summary or not summary.strip():
                    fail += 1
                    continue
                if summary.strip() == _fallback_truncation(msg).strip():
                    # summarizer backend failed -> fallback text; don't persist
                    # junk, leave row for a later pass.
                    fail += 1
                    time.sleep(args.sleep)
                    continue
                # Lock-aware write with backoff.
                for attempt in range(args.max_lock_retries):
                    try:
                        store.set_summary(mid, summary.strip())
                        ok += 1
                        break
                    except sqlite3.OperationalError as e:
                        if "database is locked" in str(e).lower():
                            wait = min(2 ** attempt, 15)
                            time.sleep(wait)
                            continue
                        raise
                else:
                    fail += 1
                    print(f"[throttled-backfill] LOCK-GIVEUP id={mid}", flush=True)
            except Exception as e:
                fail += 1
                print(f"[throttled-backfill] FAIL id={mid}: {e}", flush=True)

            if ok and ok % args.heartbeat == 0:
                rem = remaining_count()
                rate = ok / max(1e-9, (time.time() - t0)) * 60
                eta_min = rem / max(1e-9, rate)
                print(f"[throttled-backfill] ok={ok} fail={fail} remaining={rem} "
                      f"rate={rate:.1f}/min eta~{eta_min:.0f}min @ {datetime.now():%H:%M:%S}",
                      flush=True)

            time.sleep(args.sleep)

    print(f"[throttled-backfill] DONE ok={ok} fail={fail} "
          f"remaining={remaining_count()} elapsed={(time.time()-t0)/60:.1f}min",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
