#!/usr/bin/env python3
"""
context_injector.py — Assemble context from ContextGraph for session injection.

Given an incoming query (the user's first message or session topic),
assembles a context block from ContextGraph that can be injected
into the system prompt at session start.

This is the interface between ContextGraph and OpenClaw's injection layer.
The output format is designed for direct inclusion in system prompts.

Usage:
  # CLI testing
  python3 scripts/context_injector.py "what's the status of maxrisk?"
  python3 scripts/context_injector.py --budget 1500 "memory system architecture"

  # Python API (for OpenClaw integration)
  from scripts.context_injector import assemble_context
  context_block = assemble_context("user query", token_budget=2000)

Output format:
  ## Retrieved Context
  
  *Assembled by ContextGraph — 8 messages, 1847 tokens*
  *Tags: [maxrisk, trading, options]*
  
  ### [2026-03-18] MaxRisk Project Status
  ...content...
  
  ### [2026-03-17] Trading Research
  ...content...

Design constraints:
- Uses existing ContextAssembler — no reinvention
- Respects configurable token budget (default 2000)
- Output is markdown, suitable for system prompt injection
- Graceful degradation if ContextGraph is empty

Author: Agent: Mei (梅) — Tsinghua KEG Lab
"""

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from store import Message, MessageStore
from features import extract_features
from tagger import assign_tags
from assembler import ContextAssembler, AssemblyResult

# ── Configuration ────────────────────────────────────────────────────────────

DEFAULT_TOKEN_BUDGET = 2000
MAX_CONTENT_PER_MESSAGE = 400  # Truncate individual messages for density


# ── Formatting ───────────────────────────────────────────────────────────────

def _format_timestamp(ts: float) -> str:
    """Format unix timestamp as YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text with ellipsis if too long."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3].rsplit(" ", 1)[0] + "..."


def _extract_title(msg: Message) -> str:
    """Extract a title from the message for headers."""
    # For memory files, user_text is "[category] Title"
    user_text = msg.user_text.strip()
    if user_text.startswith("["):
        # Strip category prefix
        idx = user_text.find("]")
        if idx > 0:
            return user_text[idx + 1:].strip()
    # For interactive messages, use first line or truncate
    first_line = user_text.split("\n")[0]
    return _truncate(first_line, 60)


def _format_message(msg: Message, max_content: int = MAX_CONTENT_PER_MESSAGE) -> str:
    """Format a single message for context injection."""
    ts = _format_timestamp(msg.timestamp)
    title = _extract_title(msg)
    tags_str = ", ".join(msg.tags[:5])  # Limit displayed tags

    # Build content block
    lines = [f"### [{ts}] {title}"]
    if tags_str:
        lines.append(f"*Tags: {tags_str}*")

    # Include assistant response (the actual content)
    content = msg.assistant_text.strip()
    if content:
        lines.append("")
        lines.append(_truncate(content, max_content))

    return "\n".join(lines)


def format_context_block(result: AssemblyResult) -> str:
    """
    Format AssemblyResult as an injectable context block.

    Output is markdown designed for system prompt injection.
    """
    if not result.messages:
        return ""

    lines = [
        "## Retrieved Context",
        "",
        f"*Assembled by ContextGraph — {len(result.messages)} messages, "
        f"~{result.total_tokens} tokens*",
    ]

    if result.tags_used:
        tags_str = ", ".join(result.tags_used[:10])
        lines.append(f"*Query tags: [{tags_str}]*")

    lines.append("")

    # Add formatted messages (oldest first — natural reading order)
    for msg in result.messages:
        lines.append(_format_message(msg))
        lines.append("")

    return "\n".join(lines)


# ── Core Assembly ────────────────────────────────────────────────────────────

def assemble_context(
    query: str,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    pinned_ids: Optional[List[str]] = None,
) -> str:
    """
    Assemble context from ContextGraph for a given query.

    This is the main Python API for OpenClaw integration.

    Parameters:
        query: The user's incoming message or session topic
        token_budget: Maximum tokens for assembled context
        pinned_ids: Optional list of message IDs to pin in sticky layer

    Returns:
        Formatted markdown context block, or empty string if nothing found.
    """
    store = MessageStore()
    assembler = ContextAssembler(store, token_budget=token_budget)

    # Infer tags from query
    features = extract_features(query, "")
    inferred_tags = assign_tags(features, query, "")

    # Assemble context
    result = assembler.assemble(
        incoming_text=query,
        inferred_tags=inferred_tags,
        pinned_message_ids=pinned_ids,
    )

    return format_context_block(result)


def assemble_for_session(
    first_message: str,
    session_type: str = "direct",
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> dict:
    """
    Assemble context for a new session.

    Returns a dict with both the formatted block and metadata,
    suitable for OpenClaw's injection layer.

    Parameters:
        first_message: The user's first message in the session
        session_type: "direct", "subagent", "cron", etc.
        token_budget: Maximum tokens for assembled context

    Returns:
        {
            "context_block": str,  # Formatted markdown
            "tokens": int,         # Estimated tokens used
            "message_count": int,  # Number of messages retrieved
            "tags": List[str],     # Tags that matched
            "source": "contextgraph",
        }
    """
    store = MessageStore()
    assembler = ContextAssembler(store, token_budget=token_budget)

    # Infer tags from first message
    features = extract_features(first_message, "")
    inferred_tags = assign_tags(features, first_message, "")

    # Assemble
    result = assembler.assemble(
        incoming_text=first_message,
        inferred_tags=inferred_tags,
    )

    return {
        "context_block": format_context_block(result),
        "tokens": result.total_tokens,
        "message_count": len(result.messages),
        "tags": result.tags_used,
        "source": "contextgraph",
    }


def assemble_with_explicit_tags(
    tags: List[str],
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    pinned_ids: Optional[List[str]] = None,
) -> dict:
    """
    Assemble context using explicit tags, bypassing tagger inference.

    Use this when you have a known set of high-value tags and don't want
    to rely on the tagger to infer them from a query string.

    Parameters:
        tags: Explicit list of tags to retrieve context for
        token_budget: Maximum tokens for assembled context
        pinned_ids: Optional list of message IDs to pin in sticky layer

    Returns:
        {
            "context_block": str,  # Formatted markdown
            "tokens": int,         # Estimated tokens used
            "message_count": int,  # Number of messages retrieved
            "tags": List[str],     # Tags that were used
            "source": "contextgraph",
        }

    Example:
        result = assemble_with_explicit_tags(
            tags=["maxrisk", "infrastructure", "decision"],
            token_budget=1500
        )
    """
    store = MessageStore()
    assembler = ContextAssembler(store, token_budget=token_budget)

    # Assemble directly with explicit tags — no tagger inference
    result = assembler.assemble(
        incoming_text="",
        inferred_tags=tags,
        pinned_message_ids=pinned_ids,
    )

    return {
        "context_block": format_context_block(result),
        "tokens": result.total_tokens,
        "message_count": len(result.messages),
        "tags": result.tags_used,
        "source": "contextgraph",
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Assemble context from ContextGraph for session injection"
    )
    parser.add_argument("query", nargs="?", default="",
                        help="Query text (user's incoming message)")
    parser.add_argument("--budget", type=int, default=DEFAULT_TOKEN_BUDGET,
                        help=f"Token budget (default: {DEFAULT_TOKEN_BUDGET})")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON (for API integration)")
    parser.add_argument("--stats-only", action="store_true",
                        help="Print stats without full context")
    args = parser.parse_args()

    if not args.query:
        print("Usage: context_injector.py 'your query here'", file=sys.stderr)
        print("\nExamples:", file=sys.stderr)
        print("  context_injector.py 'maxrisk project status'", file=sys.stderr)
        print("  context_injector.py --budget 1500 'memory architecture'", file=sys.stderr)
        sys.exit(1)

    result = assemble_for_session(args.query, token_budget=args.budget)

    if args.json:
        import json
        print(json.dumps(result, indent=2))
    elif args.stats_only:
        print(f"Query: {args.query!r}")
        print(f"Messages: {result['message_count']}")
        print(f"Tokens: {result['tokens']}")
        print(f"Tags: {result['tags']}")
    else:
        print(f"Query: {args.query!r}")
        print(f"Budget: {args.budget} tokens")
        print("=" * 60)
        print()
        if result["context_block"]:
            print(result["context_block"])
        else:
            print("(no relevant context found)")
        print()
        print(f"Stats: {result['message_count']} messages, "
              f"~{result['tokens']} tokens, tags={result['tags']}")


if __name__ == "__main__":
    main()
