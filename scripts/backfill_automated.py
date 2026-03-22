"""
backfill_automated.py — Backfill is_automated flag for existing records.

Iterates all records in the store and applies the automated turn detection logic.
Sets is_automated=1 for matching records (cron, heartbeat, local-watcher).

Usage:
  python3 scripts/backfill_automated.py [--dry-run] [--verbose]

The script is idempotent and safe to run multiple times.
"""

import argparse
import sys
import time
from pathlib import Path

# Allow running from project root or scripts/ directory
sys.path.insert(0, str(Path(__file__).parent.parent))
from store import MessageStore
from logger import _is_automated_turn


def backfill_automated(dry_run: bool = False, verbose: bool = False) -> dict:
    """
    Backfill is_automated flag for all messages in the store.

    Parameters
    ----------
    dry_run : bool
        If True, only report what would be updated without making changes
    verbose : bool
        If True, print detailed progress information

    Returns
    -------
    dict
        Statistics about the backfill operation:
        - total_messages: total messages examined
        - automated_detected: messages identified as automated
        - already_marked: messages already marked as automated
        - newly_marked: messages newly marked as automated
    """
    store = MessageStore()
    conn = store._conn()

    # Get all messages
    if verbose:
        print("Fetching all messages from store...")

    cursor = conn.execute("SELECT COUNT(*) FROM messages")
    total_messages = cursor.fetchone()[0]

    if verbose:
        print(f"Found {total_messages} total messages")

    # Fetch all messages with is_automated flag
    cursor = conn.execute("SELECT id, user_text, is_automated FROM messages")
    rows = cursor.fetchall()

    automated_detected = 0
    already_marked = 0
    newly_marked = 0
    updates = []

    for row in rows:
        message_id = row[0]
        user_text = row[1]
        current_is_automated = bool(row[2]) if row[2] is not None else False

        # Apply detection logic
        should_be_automated = _is_automated_turn(user_text)

        if should_be_automated:
            automated_detected += 1
            if current_is_automated:
                already_marked += 1
            else:
                newly_marked += 1
                updates.append(message_id)
                if verbose:
                    print(f"  Will mark as automated: {message_id[:8]}... {user_text[:60]}")

    # Apply updates
    if not dry_run and updates:
        if verbose:
            print(f"\nUpdating {len(updates)} messages...")
        for message_id in updates:
            conn.execute(
                "UPDATE messages SET is_automated = 1 WHERE id = ?",
                (message_id,)
            )
        conn.commit()
        if verbose:
            print("Updates committed.")
    elif dry_run and updates:
        print(f"\n[DRY RUN] Would update {len(updates)} messages")

    stats = {
        "total_messages": total_messages,
        "automated_detected": automated_detected,
        "already_marked": already_marked,
        "newly_marked": newly_marked,
    }

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Backfill is_automated flag for existing records"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without making changes"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed progress information"
    )
    args = parser.parse_args()

    print("Starting automated turn backfill...")
    start_time = time.time()

    stats = backfill_automated(dry_run=args.dry_run, verbose=args.verbose)

    elapsed = time.time() - start_time

    print("\n" + "=" * 60)
    print("BACKFILL COMPLETE")
    print("=" * 60)
    print(f"Total messages examined:       {stats['total_messages']}")
    print(f"Automated turns detected:      {stats['automated_detected']}")
    print(f"  Already marked:              {stats['already_marked']}")
    print(f"  Newly marked:                {stats['newly_marked']}")
    print(f"Time elapsed:                  {elapsed:.2f}s")
    print("=" * 60)

    if args.dry_run:
        print("\n[DRY RUN] No changes were made. Run without --dry-run to apply updates.")
    elif stats['newly_marked'] > 0:
        print(f"\n✓ Successfully updated {stats['newly_marked']} messages")
    else:
        print("\n✓ All messages already have correct is_automated flags")


if __name__ == "__main__":
    main()
