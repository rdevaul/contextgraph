#!/usr/bin/env python3
"""
backfill_summaries.py — One-shot script to backfill summaries for large messages.

Iterates all messages where token_count > threshold AND summary IS NULL,
generates summaries using the configured backend, and stores them.

Usage:
    python scripts/backfill_summaries.py [--dry-run] [--limit N] [--threshold N]

Flags:
    --dry-run       Print what would be done without actually summarizing
    --limit N       Process at most N messages (default: all)
    --threshold N   Token count threshold (default: 2000)
"""

import sys
import argparse
import sqlite3
from pathlib import Path

# Add parent directory to path to import project modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from store import MessageStore, Message
from summarizer import summarize_message


def backfill_summaries(dry_run: bool = False, limit: int | None = None, threshold: int = 2000) -> None:
    """
    Backfill summaries for large messages.

    Args:
        dry_run: If True, only print what would be done
        limit: Maximum number of messages to process (None = all)
        threshold: Token count threshold for summarization
    """
    store = MessageStore()
    conn = store._conn()

    # Find messages that need summaries
    query = """
        SELECT id, token_count
        FROM messages
        WHERE token_count > ? AND (summary IS NULL OR summary = '')
        ORDER BY timestamp DESC
    """

    if limit:
        query += f" LIMIT {limit}"

    cursor = conn.execute(query, (threshold,))
    candidates = cursor.fetchall()

    total = len(candidates)
    print(f"Found {total} messages needing summaries (token_count > {threshold})")

    if total == 0:
        print("Nothing to do.")
        return

    if dry_run:
        print("\n--dry-run mode: would summarize the following messages:")
        for i, row in enumerate(candidates, 1):
            print(f"  {i}. Message ID: {row['id']}, token_count: {row['token_count']}")
        return

    # Process messages
    for i, row in enumerate(candidates, 1):
        msg_id = row['id']
        token_count = row['token_count']

        print(f"Summarizing message {i} of {total} (token_count={token_count})...")

        # Fetch full message
        msg = store.get_by_id(msg_id)
        if msg is None:
            print(f"  WARNING: Message {msg_id} not found, skipping")
            continue

        # Generate summary
        try:
            summary = summarize_message(msg)
            store.set_summary(msg_id, summary)
            print(f"  ✓ Summary stored ({len(summary)} chars)")
        except Exception as e:
            print(f"  ✗ Failed: {e}")
            continue

    print(f"\nDone! Processed {total} messages.")


def main():
    parser = argparse.ArgumentParser(
        description="Backfill summaries for large messages in the tag-context store"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without actually summarizing"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N messages (default: all)"
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=2000,
        help="Token count threshold for summarization (default: 2000)"
    )

    args = parser.parse_args()

    try:
        backfill_summaries(
            dry_run=args.dry_run,
            limit=args.limit,
            threshold=args.threshold
        )
    except KeyboardInterrupt:
        print("\n\nInterrupted by user. Exiting cleanly.")
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
