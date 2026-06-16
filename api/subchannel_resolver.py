"""
subchannel_resolver.py — Canonical subchannel identity resolution.

Problem: the contextgraph DB ends up with messages tagged by *different*
representations of the same logical pane:
  - UUID:   "ed215dcf-5dda-493e-8155-8edd2450238b"
  - slug:   "multigraph-improvements"
  - label:  "Multigraph Improvements"
  - legacy: "multigraph_improvements"

This makes cross-table joins miss rows, causing the goal watcher to receive
empty recent_turns and hallucinate goals (observed 2026-06-11).

Solution: a lightweight two-level resolver:
  1. An in-process dict (SubchannelResolver) built from the DB at startup
     and updated on every new label seen.
  2. A `subchannel_aliases` table in SQLite that persists across restarts.

Any form → canonical UUID (or the first form we saw, if no UUID was ever
registered).  The resolver is thread-safe (RLock).

Usage:
    from subchannel_resolver import get_resolver
    r = get_resolver(db_conn)
    canonical = r.resolve("multigraph-improvements")   # → UUID
    r.register("ed215dcf-...", ["multigraph-improvements", "Multigraph Improvements"])
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS subchannel_aliases (
    alias       TEXT NOT NULL,
    canonical   TEXT NOT NULL,
    registered_at REAL NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    PRIMARY KEY (alias)
);
CREATE INDEX IF NOT EXISTS idx_subchannel_aliases_canonical
    ON subchannel_aliases (canonical);
"""

UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

# Suffix patterns that mean "same pane, different session"
RESTORED_SUFFIX_RE = re.compile(
    r'(-restored(-\d+)?|-session(-\d+)?|-pane(-\d+)?|-s\d+)$',
    re.IGNORECASE,
)


def _is_uuid(s: str) -> bool:
    return bool(UUID_RE.match(s))


def _slugify(s: str) -> str:
    """Normalise a label to a lowercase slug for fuzzy matching."""
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def _strip_restored_suffix(s: str) -> str:
    """Strip -restored[-N], -session[-N], -pane[-N], -sN suffixes."""
    return RESTORED_SUFFIX_RE.sub('', s)


# ---------------------------------------------------------------------------
# SubchannelResolver
# ---------------------------------------------------------------------------

class SubchannelResolver:
    """
    Thread-safe dict-backed resolver.  canonical form is always a UUID when
    one has been registered; otherwise it's the first form seen.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        # alias → canonical
        self._map: dict[str, str] = {}
        self._ensure_schema()
        self._load_from_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, label: str) -> str:
        """Return the canonical form for *label*.  Returns *label* unchanged
        if no mapping is known (and registers it as its own canonical).

        Resolution order:
          1. Direct map lookup
          2. Slugified form lookup
          3. Strip -restored[-N]/-session[-N] suffix, then re-lookup (steps 1+2)
          4. Self-register as own canonical (unknown label)
        """
        with self._lock:
            canon = self._map.get(label)
            if canon:
                return canon
            # Try slug match
            slug = _slugify(label)
            canon = self._map.get(slug)
            if canon:
                self._register_pair(label, canon, persist=True)
                return canon
            # Try stripping restored/session suffix and re-looking up
            stripped = _strip_restored_suffix(label)
            if stripped != label:
                canon = self._map.get(stripped)
                if not canon:
                    canon = self._map.get(_slugify(stripped))
                if canon:
                    self._register_pair(label, canon, persist=True)
                    logger.info(f"[resolver] suffix-stripped: {label!r} → {canon!r}")
                    return canon
            # Unknown — register as its own canonical
            self._register_pair(label, label, persist=True)
            return label

    def register(self, canonical: str, aliases: list[str], *,
                 prefer_uuid: bool = True) -> str:
        """
        Register *canonical* and all *aliases* as equivalent.

        If *canonical* is a UUID or *prefer_uuid* is True and we already have
        a UUID registered for any of the aliases, that UUID wins.

        Returns the final canonical form used.
        """
        with self._lock:
            # Find any pre-existing UUID among canonical + aliases
            all_forms = [canonical] + list(aliases)
            existing_uuid: Optional[str] = None
            if prefer_uuid:
                for f in all_forms:
                    if _is_uuid(f):
                        existing_uuid = f
                        break
                if not existing_uuid:
                    # Look up through current map
                    for f in all_forms:
                        c = self._map.get(f) or self._map.get(_slugify(f))
                        if c and _is_uuid(c):
                            existing_uuid = c
                            break
            winner = existing_uuid or canonical
            for form in all_forms:
                if form != winner:
                    self._register_pair(form, winner, persist=True)
            self._register_pair(winner, winner, persist=True)
            logger.debug(f"[resolver] registered {len(all_forms)} aliases → {winner!r}")
            return winner

    def all_aliases_for(self, canonical: str) -> list[str]:
        """Return all known aliases that map to *canonical*."""
        with self._lock:
            return [a for a, c in self._map.items() if c == canonical]

    def canonical_for(self, label: str) -> str:
        """Alias for resolve()."""
        return self.resolve(label)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _register_pair(self, alias: str, canonical: str, *, persist: bool) -> None:
        """Map alias → canonical, and if persist, write to DB."""
        self._map[alias] = canonical
        # Also store slugified form
        slug = _slugify(alias)
        if slug != alias:
            self._map[slug] = canonical
        if persist:
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO subchannel_aliases (alias, canonical) VALUES (?,?)",
                    (alias, canonical),
                )
                self._conn.commit()
            except Exception as exc:
                logger.warning(f"[resolver] DB write failed for {alias!r}: {exc}")

    def _ensure_schema(self) -> None:
        try:
            self._conn.executescript(DDL)
            self._conn.commit()
        except Exception as exc:
            logger.warning(f"[resolver] schema init failed: {exc}")

    def _load_from_db(self) -> None:
        try:
            rows = self._conn.execute(
                "SELECT alias, canonical FROM subchannel_aliases"
            ).fetchall()
            for alias, canonical in rows:
                self._map[alias] = canonical
                slug = _slugify(alias)
                if slug != alias:
                    self._map[slug] = canonical
            logger.info(f"[resolver] loaded {len(rows)} aliases from DB")
        except Exception as exc:
            logger.warning(f"[resolver] failed to load aliases: {exc}")


# ---------------------------------------------------------------------------
# Module-level singleton (one per DB connection)
# ---------------------------------------------------------------------------

_instances: dict[int, SubchannelResolver] = {}
_instances_lock = threading.Lock()


def get_resolver(conn: sqlite3.Connection) -> SubchannelResolver:
    """Return a cached SubchannelResolver for the given connection."""
    key = id(conn)
    with _instances_lock:
        if key not in _instances:
            _instances[key] = SubchannelResolver(conn)
        return _instances[key]
