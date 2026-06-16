#!/usr/bin/env python3
"""
seed_subchannel_aliases.py — One-shot bootstrap + ongoing maintenance tool.

Scans all distinct subchannel_label values in the messages table, groups them
by their canonical base (stripping -restored-N, -session-N, -pane-N suffixes),
and registers the whole group in subchannel_aliases with the shortest slug as
canonical.

Also handles explicit cross-mappings (e.g. UUID ↔ slug) when a UUID was seen
alongside a known slug via ingest.

Safe to re-run: uses INSERT OR REPLACE.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path.home() / ".tag-context" / "store.db"

# Suffixes that are "same pane, different session"
STRIP_RE = re.compile(
    r'(-restored(-\d+)?|-session(-\d+)?|-pane(-\d+)?|-s\d+)$',
    re.IGNORECASE,
)

# Explicit manual overrides: (alias, canonical)
# Add entries here when automatic suffix-stripping isn't enough.
MANUAL_OVERRIDES: list[tuple[str, str]] = [
    # fea variants
    ("fea-restored-2",  "fea"),
    ("fea-restored-3",  "fea"),
    ("fea-restored-4",  "fea"),
    ("fea-pane",        "fea"),
    # assembly variants
    ("agentic-1-assembly-restored",  "agentic-1-assembly"),
    ("agentic-1-assm",               "agentic-1-assembly"),
    ("assm-ui-restored",             "assm-ui"),
    # multigraph variants
    ("multigraph-restored", "multigraph"),
    # hydra
    ("hydra-robot-design-restored", "hydra-robot-design"),
    ("hydra-robot-design-session",  "hydra-robot-design"),
    # yapcad
    ("yapcad-mechatron-wbs-structure-restored-2", "yapcad-mechatron-wbs-structure"),
    # channel name drift
    ("jarvis-rich",    "sybilclaw"),
]


def seed(db_path: Path = DB_PATH, dry_run: bool = False) -> None:
    conn = sqlite3.connect(str(db_path))

    # Ensure table exists
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subchannel_aliases (
            alias         TEXT NOT NULL,
            canonical     TEXT NOT NULL,
            registered_at REAL NOT NULL DEFAULT (unixepoch('now', 'subsec')),
            PRIMARY KEY (alias)
        );
        CREATE INDEX IF NOT EXISTS idx_subchannel_aliases_canonical
            ON subchannel_aliases (canonical);
    """)
    conn.commit()

    # Gather all distinct labels from messages
    labels: list[str] = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT subchannel_label FROM messages WHERE subchannel_label IS NOT NULL"
        ).fetchall()
    ]

    # Group by stripped base
    groups: dict[str, list[str]] = {}
    for label in labels:
        base = STRIP_RE.sub("", label)
        groups.setdefault(base, []).append(label)

    # Build pairs: (alias, canonical)
    pairs: list[tuple[str, str]] = []

    for base, members in groups.items():
        # Canonical = shortest member (usually the clean slug without suffix)
        canonical = min(members, key=len)
        for m in members:
            pairs.append((m, canonical))
        # Also register the base itself → canonical (in case base ≠ shortest)
        pairs.append((base, canonical))

    # Add manual overrides (these win over automatic grouping)
    pairs.extend(MANUAL_OVERRIDES)

    # Also self-register every canonical so it always resolves
    canonicals = {c for _, c in pairs}
    for c in canonicals:
        pairs.append((c, c))

    # Deduplicate, keeping last writer (manual overrides are appended last so
    # they win when we do the INSERT OR REPLACE pass in order)
    seen: dict[str, str] = {}
    for alias, canonical in pairs:
        seen[alias] = canonical

    if dry_run:
        print(f"[dry-run] Would write {len(seen)} aliases:")
        for alias, canonical in sorted(seen.items()):
            flag = " (SELF)" if alias == canonical else ""
            print(f"  {alias!r:55s} → {canonical!r}{flag}")
        conn.close()
        return

    inserted = 0
    updated = 0
    for alias, canonical in seen.items():
        existing = conn.execute(
            "SELECT canonical FROM subchannel_aliases WHERE alias=?", (alias,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO subchannel_aliases (alias, canonical) VALUES (?,?)",
                (alias, canonical),
            )
            inserted += 1
        elif existing[0] != canonical:
            conn.execute(
                "UPDATE subchannel_aliases SET canonical=? WHERE alias=?",
                (canonical, alias),
            )
            updated += 1

    conn.commit()
    conn.close()

    total = len(seen)
    print(f"[seed_subchannel_aliases] Done: {total} total, {inserted} inserted, {updated} updated.")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    seed(dry_run=dry)
