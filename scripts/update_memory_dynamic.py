#!/usr/bin/env python3
"""
update_memory_dynamic.py — Inject ContextGraph context into MEMORY.md (or shadow file).

Queries the ContextGraph /assemble endpoint with a broad query, then writes the
result into a DYNAMIC_CONTEXT section in the target memory file.

Usage:
  python3 scripts/update_memory_dynamic.py [--shadow] [--query TEXT] [--budget TOKENS] [--dry-run]

  --shadow     Write to SHADOWMEMORY.md instead of MEMORY.md (default: shadow)
  --live       Write to actual MEMORY.md
  --query TEXT Override default broad query
  --budget N   Token budget for assembly (default: 1500)
  --dry-run    Print result without writing

The script is safe: if /assemble returns empty, it skips the write entirely.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
import urllib.request
import urllib.error

# Add parent/scripts directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
import config
from channel_access import filter_turns_for_agent

WORKSPACE = config.WORKSPACE
MEMORY_FILE = config.MEMORY_FILE
SHADOW_FILE = WORKSPACE / "SHADOWMEMORY.md"
CONTEXT_API = "http://localhost:8300/assemble"

DEFAULT_QUERY = "recent projects decisions infrastructure voice PWA context graph memory"
DEFAULT_BUDGET = 8000  # Recent messages often 2000+ tokens; needs headroom

# Explicit tags for MEMORY.md injection — bypasses tagger inference which
# fails on abstract query strings. The /assemble endpoint accepts a `tags`
# parameter that overrides tagger inference (Rich's hook in api/server.py).
DEFAULT_TAGS = [
    "decision", "infrastructure", "deployment", "security", "research",
    "agents", "framework1", "maxrisk", "eldrchat", "contextgraph", "planning",
]

SECTION_START = "<!-- DYNAMIC_CONTEXT_START -->"
SECTION_END = "<!-- DYNAMIC_CONTEXT_END -->"


def assemble(query: str, token_budget: int, tags: list[str] | None = None) -> dict:
    """
    Call /assemble and return the response dict.
    
    If tags is provided, bypasses tagger inference and uses explicit tags directly.
    This is Rich's hook — see api/server.py AssembleRequest.tags field.
    """
    payload_dict = {"user_text": query, "token_budget": token_budget}
    if tags:
        payload_dict["tags"] = tags
    payload = json.dumps(payload_dict).encode()
    req = urllib.request.Request(
        CONTEXT_API,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"[update_memory_dynamic] ERROR: Could not reach {CONTEXT_API}: {e}", file=sys.stderr)
        return {}


def _clean_user_text(text: str) -> str:
    """Strip OpenClaw metadata envelopes from user message text."""
    # Strip "Conversation info (untrusted metadata): ```json ... ```"
    text = re.sub(
        r"Conversation info \(untrusted metadata\):\s*```json\s*\{.*?\}\s*```\s*",
        "", text, flags=re.DOTALL
    )
    # Strip "Sender (untrusted metadata): ```json ... ```"
    text = re.sub(
        r"Sender \(untrusted metadata\):\s*```json\s*\{.*?\}\s*```\s*",
        "", text, flags=re.DOTALL
    )
    # Strip "Replied message ..." blocks
    text = re.sub(
        r"Replied message \(untrusted.*?\):\s*```json\s*\{.*?\}\s*```\s*",
        "", text, flags=re.DOTALL
    )
    # Strip voice PWA prefix
    text = re.sub(r"^\[.*?\]\s*\[Voice PWA\]\s*", "", text)
    # Strip media attachment lines
    text = re.sub(r"\[media attached:.*?\]\s*", "", text)
    # Strip queued messages block
    text = re.sub(r"\[Queued messages while agent was busy\].*", "", text, flags=re.DOTALL)
    return text.strip()


def format_dynamic_section(result: dict, query: str) -> str:
    """Format the assembled context as a markdown section."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()
    messages = result.get("messages", [])
    total_tokens = result.get("total_tokens", 0)
    tags_used = result.get("tags_used", [])

    lines = [
        f"\n## Dynamic Context (auto-updated {ts})",
        f"*Query: `{query}` | {len(messages)} messages | {total_tokens} tokens | tags: {', '.join(tags_used)}*\n",
    ]

    for msg in messages:
        user_text = _clean_user_text(msg.get("user_text", "").strip())
        assistant_text = msg.get("assistant_text", "").strip()
        tags = msg.get("tags", [])

        # Skip messages where user_text is empty after cleaning (pure metadata)
        if not user_text:
            continue

        # Trim very long texts
        if len(user_text) > 300:
            user_text = user_text[:300] + "…"
        if len(assistant_text) > 500:
            assistant_text = assistant_text[:500] + "…"

        lines.append(f"**[{', '.join(tags)}]**")
        lines.append(f"- *Q:* {user_text}")
        lines.append(f"- *A:* {assistant_text}")
        lines.append("")

    return "\n".join(lines)


def inject_into_file(target: Path, section_content: str, dry_run: bool = False) -> bool:
    """
    Inject dynamic section into target file between marker comments.
    If markers don't exist, append to end of file.
    Returns True if file was changed.
    """
    if target.exists():
        original = target.read_text(encoding="utf-8")
    else:
        original = ""

    new_section = f"{SECTION_START}\n{section_content}\n{SECTION_END}"

    if SECTION_START in original and SECTION_END in original:
        # Replace existing section
        pattern = re.compile(
            re.escape(SECTION_START) + r".*?" + re.escape(SECTION_END),
            re.DOTALL
        )
        updated = pattern.sub(new_section, original)
    else:
        # Append to end
        updated = original.rstrip() + "\n\n" + new_section + "\n"

    if updated == original:
        return False

    if dry_run:
        print("--- DRY RUN: would write to", target)
        print(new_section[:2000])
        return True

    target.write_text(updated, encoding="utf-8")
    return True


def main():
    parser = argparse.ArgumentParser(description="Update MEMORY.md with ContextGraph dynamic context")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--shadow", action="store_true", default=True,
                      help="Write to SHADOWMEMORY.md (default)")
    mode.add_argument("--live", action="store_true",
                      help="Write to actual MEMORY.md")
    parser.add_argument("--query", default=DEFAULT_QUERY,
                        help="Query for context assembly")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                        help="Token budget for assembly")
    parser.add_argument("--tags", nargs="*", default=None,
                        help="Explicit tags (bypasses tagger). Empty = DEFAULT_TAGS")
    parser.add_argument("--no-tags", action="store_true",
                        help="Disable explicit tags — use tagger inference (original behavior)")
    parser.add_argument("--agent-id", default=None,
                        help="Agent ID for channel-based filtering (e.g. glados-dana). "
                             "When set, only turns with matching channel labels are included.")
    parser.add_argument("--output-file", default=None,
                        help="Override output file path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print result without writing")
    args = parser.parse_args()

    if args.output_file:
        target = Path(args.output_file)
    else:
        target = MEMORY_FILE if args.live else SHADOW_FILE

    # Determine which tags to use (default: explicit tags via Rich's hook)
    if args.no_tags:
        tags = None
        tags_mode = "tagger inference"
    elif args.tags is not None:
        tags = args.tags if args.tags else DEFAULT_TAGS
        tags_mode = f"explicit ({len(tags)} tags)"
    else:
        tags = DEFAULT_TAGS
        tags_mode = f"default ({len(DEFAULT_TAGS)} tags)"

    print(f"[update_memory_dynamic] Querying ContextGraph ({tags_mode})...")
    result = assemble(args.query, args.budget, tags=tags)

    if not result:
        print("[update_memory_dynamic] ERROR: No response from ContextGraph. Skipping write.")
        sys.exit(1)

    messages = result.get("messages", [])

    # Apply per-agent channel filtering if --agent-id is provided
    if args.agent_id:
        original_count = len(messages)
        messages = filter_turns_for_agent(messages, args.agent_id)
        result["messages"] = messages
        print(f"[update_memory_dynamic] Agent '{args.agent_id}' filter: {original_count} → {len(messages)} messages")

    if not messages:
        print(f"[update_memory_dynamic] ContextGraph returned 0 messages after filtering. Skipping write.")
        sys.exit(0)

    section = format_dynamic_section(result, args.query)
    changed = inject_into_file(target, section, dry_run=args.dry_run)

    if changed and not args.dry_run:
        print(f"[update_memory_dynamic] ✅ Wrote {len(messages)} messages ({result.get('total_tokens',0)} tokens) → {target}")
    elif not changed:
        print("[update_memory_dynamic] No changes needed.")
    # dry_run output already printed in inject_into_file


if __name__ == "__main__":
    main()
