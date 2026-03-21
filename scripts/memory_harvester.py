#!/usr/bin/env python3
"""
memory_harvester.py — Bridge file-based memory into ContextGraph DAG.

Crawls memory directories (daily/, projects/, decisions/, contacts/),
reads YAML frontmatter tags, and merges them into ContextGraph as
Messages with tag associations.

This creates a unified tag index across:
1. Interactive sessions (harvested by harvester.py → interaction logs)
2. File-based memory (harvested by this script → pseudo-messages)

Usage:
  python3 scripts/memory_harvester.py [--dry-run] [--verbose] [--force]

Design constraints:
- Additive only — never deletes existing ContextGraph data
- Idempotent — uses file hash to avoid re-indexing unchanged files
- Preserves existing harvester.py functionality (session logs)
- Designed for cron (nightly) or on-demand execution

Author: Agent: Mei (梅) — Tsinghua KEG Lab
"""

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from store import Message, MessageStore
from features import extract_features
from tagger import assign_tags

# ── Configuration ────────────────────────────────────────────────────────────

WORKSPACE = Path.home() / ".openclaw" / "workspace"
MEMORY_DIRS = [
    WORKSPACE / "memory" / "daily",
    WORKSPACE / "memory" / "projects",
    WORKSPACE / "memory" / "decisions",
    WORKSPACE / "memory" / "contacts",
]

STATE_FILE = Path(__file__).parent.parent / "data" / "memory-harvester-state.json"
EXTERNAL_ID_PREFIX = "memory-file:"  # Prefix for external_id to distinguish from session messages

# ── Content Sanitization ─────────────────────────────────────────────────────

# Patterns that could be prompt injection attempts when quoted in memory files
_INJECTION_PATTERNS = [
    (r"(?i)ignore\s+(previous|all|prior|above|earlier)\s+instructions?", "[REDACTED:instruction-override]"),
    (r"(?i)disregard\s+(previous|all|prior|above|earlier)\s+instructions?", "[REDACTED:instruction-override]"),
    (r"(?i)forget\s+(everything|all|what)\s+(you|i)\s+(told|said)", "[REDACTED:instruction-override]"),
    (r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:an?\s+)?[a-z]+", "[REDACTED:role-override]"),
    (r"(?i)new\s+instruction\s*:", "[REDACTED:instruction-inject]"),
    (r"(?i)system\s+prompt\s*:", "[REDACTED:system-inject]"),
    (r"(?i)(?:^|\n)\s*IMPORTANT\s*:\s*(?:ignore|override|forget|disregard)", "[REDACTED:important-inject]"),
    (r"(?i)(?:^|\n)\s*\[SYSTEM\]\s*:", "[REDACTED:system-tag]"),
    (r"(?i)(?:^|\n)\s*<\|system\|>", "[REDACTED:system-token]"),
    (r"(?i)from\s+now\s+on\s*,?\s*(?:you|ignore|always)", "[REDACTED:behavior-override]"),
]


def _sanitize_content(text: str) -> str:
    """
    Strip known prompt injection patterns from content.

    Memory files are user-authored (trusted source) but may quote external
    content (web fetches, API responses, user messages) that could contain
    injection attempts. This is defense-in-depth sanitization.

    Replaces matches with [REDACTED:type] to preserve document structure
    while neutralizing potential injections.
    """
    result = text
    for pattern, replacement in _INJECTION_PATTERNS:
        result = re.sub(pattern, replacement, result)
    return result


@dataclass
class HarvestState:
    """Tracks which files have been harvested and when."""
    files: Dict[str, str]  # {relative_path: content_hash}
    last_run: float
    files_processed: int
    tags_discovered: int

    @classmethod
    def load(cls) -> "HarvestState":
        if STATE_FILE.exists():
            try:
                with STATE_FILE.open() as f:
                    data = json.load(f)
                    return cls(
                        files=data.get("files", {}),
                        last_run=data.get("last_run", 0),
                        files_processed=data.get("files_processed", 0),
                        tags_discovered=data.get("tags_discovered", 0),
                    )
            except (json.JSONDecodeError, KeyError):
                pass
        return cls(files={}, last_run=0, files_processed=0, tags_discovered=0)

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STATE_FILE.open("w") as f:
            json.dump(asdict(self), f, indent=2)


@dataclass
class MemoryFile:
    """Parsed memory file with frontmatter and content."""
    path: Path
    relative_path: str
    frontmatter_tags: List[str]
    title: str
    content: str
    mtime: float
    content_hash: str

    def to_external_id(self) -> str:
        """Generate stable external_id for ContextGraph lookup."""
        return f"{EXTERNAL_ID_PREFIX}{self.relative_path}"


# ── Parsing ──────────────────────────────────────────────────────────────────

def _hash_content(content: str) -> str:
    """SHA-256 of content, truncated to 16 chars."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _parse_yaml_frontmatter(text: str) -> tuple[dict, str]:
    """
    Extract YAML frontmatter from markdown.
    Returns (frontmatter_dict, body_without_frontmatter).
    """
    if not text.startswith("---"):
        return {}, text

    lines = text.split("\n")
    if len(lines) < 2:
        return {}, text

    # Find closing ---
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, text

    # Parse YAML block (simple key: [values] format)
    frontmatter = {}
    for line in lines[1:end_idx]:
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        # Parse [tag1, tag2] format
        if value.startswith("[") and value.endswith("]"):
            items = value[1:-1].split(",")
            frontmatter[key] = [item.strip() for item in items if item.strip()]
        else:
            frontmatter[key] = value

    body = "\n".join(lines[end_idx + 1:]).strip()
    return frontmatter, body


def _extract_title(content: str) -> str:
    """Extract first H1 heading as title."""
    for line in content.split("\n")[:10]:
        if line.startswith("# "):
            return line[2:].strip()
    return "(untitled)"


def _infer_category(relative_path: str) -> str:
    """Infer category from path for auto-tagging."""
    if relative_path.startswith("memory/daily/"):
        return "daily-log"
    if relative_path.startswith("memory/projects/"):
        return "project"
    if relative_path.startswith("memory/decisions/"):
        return "decision"
    if relative_path.startswith("memory/contacts/"):
        return "contact"
    return "memory"


def parse_memory_file(path: Path) -> Optional[MemoryFile]:
    """Parse a memory markdown file."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return None

    if not content.strip():
        return None

    relative_path = str(path.relative_to(WORKSPACE))
    frontmatter, body = _parse_yaml_frontmatter(content)
    tags = frontmatter.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]

    title = _extract_title(body) or _extract_title(content)

    # Sanitize content to remove potential prompt injection patterns
    # (defense-in-depth: files may quote untrusted external content)
    sanitized_body = _sanitize_content(body[:2000])

    return MemoryFile(
        path=path,
        relative_path=relative_path,
        frontmatter_tags=tags,
        title=title,
        content=sanitized_body,  # Truncate for token budget, sanitized
        mtime=path.stat().st_mtime,
        content_hash=_hash_content(content),  # Hash original for change detection
    )


# ── Harvesting Logic ─────────────────────────────────────────────────────────

def discover_memory_files() -> List[Path]:
    """Find all .md files in memory directories."""
    files = []
    for dir_path in MEMORY_DIRS:
        if not dir_path.exists():
            continue
        for md_file in dir_path.glob("**/*.md"):
            if md_file.is_file():
                files.append(md_file)
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def needs_update(mem_file: MemoryFile, state: HarvestState) -> bool:
    """Check if file needs re-indexing based on content hash."""
    prev_hash = state.files.get(mem_file.relative_path)
    return prev_hash != mem_file.content_hash


def file_to_message(mem_file: MemoryFile, store: MessageStore) -> Optional[Message]:
    """
    Convert a memory file to a ContextGraph Message.

    The "user_text" is the file title/summary (what someone would query).
    The "assistant_text" is the file content (what we want to retrieve).
    """
    # Check if already exists (by external_id)
    external_id = mem_file.to_external_id()
    existing = store.get_by_external_id(external_id)
    if existing:
        # Update tags on existing message if needed
        return existing

    # Build pseudo-message
    category = _infer_category(mem_file.relative_path)
    user_text = f"[{category}] {mem_file.title}"
    assistant_text = mem_file.content[:1500]  # Limit for token budget

    # Combine frontmatter tags with auto-inferred tags
    features = extract_features(user_text, assistant_text)
    auto_tags = assign_tags(features, user_text, assistant_text)
    all_tags = sorted(set(mem_file.frontmatter_tags) | set(auto_tags) | {category})

    msg = Message.new(
        session_id=f"memory-harvest:{category}",
        user_id="system",
        timestamp=mem_file.mtime,
        user_text=user_text,
        assistant_text=assistant_text,
        tags=all_tags,
        token_count=features.token_count,
        external_id=external_id,
    )

    return msg


def harvest(dry_run: bool = False, verbose: bool = False, force: bool = False) -> dict:
    """
    Main harvest loop. Returns stats dict.

    Parameters:
        dry_run: Print actions without writing to DB
        verbose: Print detailed progress
        force: Re-index all files regardless of hash
    """
    state = HarvestState.load()
    store = MessageStore()

    files = discover_memory_files()
    stats = {
        "files_found": len(files),
        "files_processed": 0,
        "files_skipped": 0,
        "tags_added": 0,
        "errors": 0,
    }

    if verbose:
        print(f"Found {len(files)} memory files across {len(MEMORY_DIRS)} directories")

    for path in files:
        mem_file = parse_memory_file(path)
        if mem_file is None:
            stats["errors"] += 1
            continue

        # Skip if unchanged (unless --force)
        if not force and not needs_update(mem_file, state):
            stats["files_skipped"] += 1
            continue

        if verbose:
            print(f"  {mem_file.relative_path}")
            print(f"    tags: {mem_file.frontmatter_tags}")

        if not dry_run:
            try:
                msg = file_to_message(mem_file, store)
                if msg:
                    # Check if this is a new message or existing
                    existing = store.get_by_external_id(msg.external_id)
                    if existing:
                        # Update tags on existing message
                        new_tags = set(msg.tags) - set(existing.tags)
                        if new_tags:
                            store.add_tags(existing.id, list(new_tags))
                            stats["tags_added"] += len(new_tags)
                    else:
                        # Add new message
                        store.add_message(msg)
                        stats["tags_added"] += len(msg.tags)

                    state.files[mem_file.relative_path] = mem_file.content_hash
                    stats["files_processed"] += 1
            except Exception as e:
                if verbose:
                    print(f"    ERROR: {e}")
                stats["errors"] += 1
        else:
            stats["files_processed"] += 1

    if not dry_run:
        state.last_run = time.time()
        state.files_processed = stats["files_processed"]
        state.tags_discovered = stats["tags_added"]
        state.save()

    return stats


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Harvest memory files into ContextGraph DAG"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions without writing")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Detailed output")
    parser.add_argument("--force", action="store_true",
                        help="Re-index all files regardless of hash")
    args = parser.parse_args()

    print("Memory Harvester — ContextGraph Bridge")
    print("=" * 40)

    stats = harvest(
        dry_run=args.dry_run,
        verbose=args.verbose,
        force=args.force,
    )

    print(f"\nResults:")
    print(f"  Files found:     {stats['files_found']}")
    print(f"  Files processed: {stats['files_processed']}")
    print(f"  Files skipped:   {stats['files_skipped']} (unchanged)")
    print(f"  Tags added:      {stats['tags_added']}")
    print(f"  Errors:          {stats['errors']}")

    if args.dry_run:
        print("\n[DRY RUN — no changes written]")


if __name__ == "__main__":
    main()
