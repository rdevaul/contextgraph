"""
cli.py — Simple CLI for testing the tag-context prototype.

Usage:
  python cli.py add       "user text" "assistant text" [--tags t1 t2]
  python cli.py query     "incoming message"
  python cli.py tags
  python cli.py recent    [--n 10]
  python cli.py show      <message_id>
"""

import argparse
import sys
import time
import uuid
from pathlib import Path

from store import Message, MessageStore
from features import extract_features
from tagger import assign_tags
from assembler import ContextAssembler
import config


DB_PATH = str(config.DB_PATH)


def cmd_add(args, store: MessageStore) -> None:
    """Add a message/response pair to the store."""
    features = extract_features(args.user_text, args.assistant_text)
    auto_tags = assign_tags(features, args.user_text, args.assistant_text)
    explicit_tags = args.tags or []
    all_tags = sorted(set(auto_tags) | set(explicit_tags))

    msg = Message.new(
        session_id=args.session or "default",
        user_id=args.user or "user",
        timestamp=time.time(),
        user_text=args.user_text,
        assistant_text=args.assistant_text,
        tags=all_tags,
        token_count=features.token_count,
    )
    store.add_message(msg)
    print(f"Added message {msg.id}")
    print(f"  Auto tags:    {auto_tags}")
    print(f"  Explicit tags: {explicit_tags}")
    print(f"  Final tags:   {all_tags}")
    print(f"  Token count:  {msg.token_count}")


def cmd_query(args, store: MessageStore) -> None:
    """Assemble context for an incoming message and print it."""
    features = extract_features(args.text, "")
    inferred_tags = assign_tags(features, args.text, "")
    assembler = ContextAssembler(store, token_budget=args.budget)
    result = assembler.assemble(args.text, inferred_tags)

    print(f"Query:         {args.text!r}")
    print(f"Inferred tags: {inferred_tags}")
    print(f"Context:       {len(result.messages)} messages "
          f"({result.total_tokens} tokens, "
          f"{result.recency_count} recency + {result.topic_count} topic)")
    print(f"Tags used:     {result.tags_used}")
    print()
    for i, msg in enumerate(result.messages, 1):
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(msg.timestamp))
        print(f"  [{i}] {ts}  tags={msg.tags}  tokens={msg.token_count}")
        print(f"       U: {msg.user_text[:80]!r}")
        print(f"       A: {msg.assistant_text[:80]!r}")
        print()


def cmd_tags(args, store: MessageStore) -> None:
    """List all tags with message counts."""
    counts = store.tag_counts()
    if not counts:
        print("No tags yet.")
        return
    print(f"{'Tag':<30} {'Count':>6}")
    print("-" * 38)
    for tag, count in counts.items():
        print(f"{tag:<30} {count:>6}")


def cmd_recent(args, store: MessageStore) -> None:
    """Show recent messages."""
    msgs = store.get_recent(args.n)
    if not msgs:
        print("No messages yet.")
        return
    for msg in msgs:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(msg.timestamp))
        print(f"{msg.id[:8]}  {ts}  tags={msg.tags}")
        print(f"  U: {msg.user_text[:80]!r}")
        print()


def cmd_show(args, store: MessageStore) -> None:
    """Show a single message by ID (prefix match)."""
    msg = store.get_by_id(args.id)
    if msg is None:
        print(f"Message not found: {args.id}")
        sys.exit(1)
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(msg.timestamp))
    print(f"ID:       {msg.id}")
    print(f"Session:  {msg.session_id}")
    print(f"User:     {msg.user_id}")
    print(f"Time:     {ts}")
    print(f"Tags:     {msg.tags}")
    print(f"Tokens:   {msg.token_count}")
    print(f"\nUser:\n{msg.user_text}")
    print(f"\nAssistant:\n{msg.assistant_text}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="tag-context prototype CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite DB")
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="Add a message/response pair")
    p_add.add_argument("user_text")
    p_add.add_argument("assistant_text")
    p_add.add_argument("--tags", nargs="+", metavar="TAG")
    p_add.add_argument("--session", default="default")
    p_add.add_argument("--user", default="user")

    # query
    p_query = sub.add_parser("query", help="Assemble context for incoming text")
    p_query.add_argument("text")
    p_query.add_argument("--budget", type=int, default=4000)

    # tags
    sub.add_parser("tags", help="List all tags with counts")

    # recent
    p_recent = sub.add_parser("recent", help="Show recent messages")
    p_recent.add_argument("--n", type=int, default=10)

    # show
    p_show = sub.add_parser("show", help="Show a message by ID")
    p_show.add_argument("id")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    store = MessageStore(db_path=args.db)
    dispatch = {
        "add":    cmd_add,
        "query":  cmd_query,
        "tags":   cmd_tags,
        "recent": cmd_recent,
        "show":   cmd_show,
    }
    dispatch[args.command](args, store)


if __name__ == "__main__":
    main()
