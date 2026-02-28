"""
replay.py — Load interaction logs into a MessageStore with fresh tagging.

This is the key experiment tool: replay the same log through different
tagging strategies and compare context assembly results.

Usage:
  python3 scripts/replay.py [--db PATH] [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                             [--dry-run] [--verbose]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from logger import iter_records
from store import Message, MessageStore
from features import extract_features
from tagger import assign_tags
from ensemble import EnsembleTagger
from gp_tagger import GeneticTagger


def replay(db_path: str, start_date: str | None, end_date: str | None,
           dry_run: bool, verbose: bool) -> None:
    store = MessageStore(db_path=db_path)

    # Build ensemble: baseline + GP tagger (if available)
    ensemble = EnsembleTagger(vote_threshold=0.3)
    ensemble.register("v0-baseline", assign_tags, initial_weight=1.0)
    gp_path = Path(__file__).parent.parent / "data" / "gp-tagger.pkl"
    if gp_path.exists():
        import pickle
        with gp_path.open("rb") as f:
            gp_tagger = pickle.load(f)
        ensemble.register(gp_tagger.tagger_id, gp_tagger.assign, initial_weight=0.8)
        if verbose:
            print(f"  Ensemble: baseline + GP tagger ({gp_tagger.tagger_id})")
    else:
        if verbose:
            print("  Ensemble: baseline only (no GP tagger found)")

    total = skipped = loaded = 0
    for record in iter_records(start_date=start_date, end_date=end_date):
        total += 1
        # Skip very short exchanges (likely system noise)
        if len(record.user_text.strip()) < 10 or len(record.assistant_text.strip()) < 10:
            skipped += 1
            continue

        # Check if already in store
        existing = store.get_by_id(record.id)
        if existing is not None:
            skipped += 1
            continue

        # Tag with ensemble
        features = extract_features(record.user_text, record.assistant_text)
        ens_result = ensemble.assign(features, record.user_text, record.assistant_text)
        tags = ens_result.tags

        msg = Message(
            id=record.id,
            session_id=record.session_id,
            user_id=record.user_id,
            timestamp=record.interaction_at,
            user_text=record.user_text,
            assistant_text=record.assistant_text,
            tags=tags,
            token_count=record.token_count,
        )

        if verbose:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.interaction_at))
            print(f"  [{ts}] [{record.channel}] tags={tags}")
            print(f"    U: {record.user_text[:70]!r}")

        if not dry_run:
            store.add_message(msg)
        loaded += 1

    print(f"\nReplay complete: {total} records, {loaded} loaded, {skipped} skipped")
    if not dry_run:
        all_tags = store.get_all_tags()
        print(f"Tag vocabulary: {len(all_tags)} tags — {all_tags}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay interaction logs into a MessageStore")
    parser.add_argument("--db", default=str(Path.home() / ".tag-context" / "store.db"),
                        help="Target SQLite DB path")
    parser.add_argument("--since", metavar="YYYY-MM-DD")
    parser.add_argument("--until", metavar="YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="Analyse without writing")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    replay(
        db_path=args.db,
        start_date=args.since,
        end_date=args.until,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
