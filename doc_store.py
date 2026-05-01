"""
doc_store.py — SQLite layer for the Whiteboard document relationship graph.

Schema (v0.2 per proposals/wikilink-relationships-02.md §2.4):
  - docs:        one row per markdown file under the vault
  - edges:       wikilink relationship edges (resolved or ghost)
  - ghost_links: aggregate count of unresolved wikilink targets
  - docs_fts:    FTS5 virtual table over (doc_id, title, body_text)

All writes go through a single connection per process. Multi-row writes use
BEGIN IMMEDIATE transactions for atomicity. Hash (sha256 of file bytes) is the
source of truth for change detection; mtime is captured but only informational.

doc_id convention: relative path from vault root, with `/` separators,
INCLUDING the `.md` extension. e.g. "projects/agentic-1.md".
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


# ---------- schema ----------

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS docs (
    doc_id           TEXT PRIMARY KEY,
    abs_path         TEXT NOT NULL,
    type             TEXT,
    status           TEXT,
    title            TEXT,
    aliases          TEXT,                              -- JSON array
    body_text        TEXT,
    hash             TEXT NOT NULL,
    mtime            REAL NOT NULL,
    indexed_at       REAL NOT NULL,
    external_source  TEXT,
    external_kind    TEXT,
    is_reference     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_docs_type      ON docs(type);
CREATE INDEX IF NOT EXISTS idx_docs_status    ON docs(status);
CREATE INDEX IF NOT EXISTS idx_docs_reference ON docs(is_reference);

CREATE TABLE IF NOT EXISTS edges (
    src_doc_id  TEXT NOT NULL,
    rel_type    TEXT NOT NULL,
    dst_name    TEXT NOT NULL,        -- raw wikilink target as written
    dst_doc_id  TEXT,                  -- resolved doc_id, NULL = ghost link
    PRIMARY KEY (src_doc_id, rel_type, dst_name),
    FOREIGN KEY (src_doc_id) REFERENCES docs(doc_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_edges_dst_id   ON edges(dst_doc_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst_name ON edges(dst_name);
CREATE INDEX IF NOT EXISTS idx_edges_rel_type ON edges(rel_type);

CREATE TABLE IF NOT EXISTS ghost_links (
    dst_name    TEXT PRIMARY KEY,
    ref_count   INTEGER NOT NULL,
    first_seen  REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    doc_id      UNINDEXED,
    title,
    body_text
);
"""


@dataclass
class DocRow:
    """In-memory representation of a row in the docs table."""
    doc_id: str
    abs_path: str
    type: Optional[str] = None
    status: Optional[str] = None
    title: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    body_text: str = ""
    hash: str = ""
    mtime: float = 0.0
    indexed_at: float = 0.0
    external_source: Optional[str] = None
    external_kind: Optional[str] = None
    is_reference: bool = False


@dataclass
class EdgeRow:
    """In-memory representation of an edge."""
    src_doc_id: str
    rel_type: str
    dst_name: str            # raw wikilink target (e.g. "agentic-1")
    dst_doc_id: Optional[str] = None  # resolved or None (ghost)


# ---------- store ----------

class DocStore:
    """SQLite-backed store for the document graph.

    Thread-safe via a single re-entrant lock (matches the existing store.py
    pattern in this repo). One connection per process; callers should not
    share DocStore across processes.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        # Ensure parent dir exists for non-`:memory:` paths
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- doc ops ----------

    def get_doc(self, doc_id: str) -> Optional[DocRow]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM docs WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return _row_to_doc(row) if row else None

    def get_doc_hash(self, doc_id: str) -> Optional[str]:
        """Cheap hash lookup for the cold-start hash-skip fast path."""
        with self._lock:
            row = self._conn.execute(
                "SELECT hash FROM docs WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return row["hash"] if row else None

    def upsert_doc_with_edges(
        self,
        doc: DocRow,
        edges: List[EdgeRow],
    ) -> None:
        """Atomic upsert of one doc + replacement of its outgoing edges.

        Single transaction. Rebuilds FTS row. Recomputes ghost_links impact:
          - for any old edge whose dst_name was unresolved, decrement ghost_links.
          - for any new edge whose dst_doc_id is NULL, increment ghost_links.
        """
        aliases_json = json.dumps(doc.aliases or [])
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                # 1. Capture old ghost edges for this src to reverse their counts
                old_ghosts = [
                    r["dst_name"]
                    for r in cur.execute(
                        "SELECT dst_name FROM edges WHERE src_doc_id = ? AND dst_doc_id IS NULL",
                        (doc.doc_id,),
                    ).fetchall()
                ]

                # 2. Upsert docs row
                cur.execute(
                    """
                    INSERT INTO docs (
                        doc_id, abs_path, type, status, title, aliases,
                        body_text, hash, mtime, indexed_at,
                        external_source, external_kind, is_reference
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(doc_id) DO UPDATE SET
                        abs_path        = excluded.abs_path,
                        type            = excluded.type,
                        status          = excluded.status,
                        title           = excluded.title,
                        aliases         = excluded.aliases,
                        body_text       = excluded.body_text,
                        hash            = excluded.hash,
                        mtime           = excluded.mtime,
                        indexed_at      = excluded.indexed_at,
                        external_source = excluded.external_source,
                        external_kind   = excluded.external_kind,
                        is_reference    = excluded.is_reference
                    """,
                    (
                        doc.doc_id, doc.abs_path, doc.type, doc.status,
                        doc.title, aliases_json, doc.body_text, doc.hash,
                        doc.mtime, doc.indexed_at,
                        doc.external_source, doc.external_kind,
                        1 if doc.is_reference else 0,
                    ),
                )

                # 3. FTS upsert: delete old row, insert new
                cur.execute("DELETE FROM docs_fts WHERE doc_id = ?", (doc.doc_id,))
                cur.execute(
                    "INSERT INTO docs_fts (doc_id, title, body_text) VALUES (?,?,?)",
                    (doc.doc_id, doc.title or "", doc.body_text or ""),
                )

                # 4. Replace edges for this src
                cur.execute("DELETE FROM edges WHERE src_doc_id = ?", (doc.doc_id,))
                for e in edges:
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO edges (src_doc_id, rel_type, dst_name, dst_doc_id)
                        VALUES (?,?,?,?)
                        """,
                        (e.src_doc_id, e.rel_type, e.dst_name, e.dst_doc_id),
                    )

                # 5. Reverse old ghost-link counts (this src no longer references them)
                for name in old_ghosts:
                    cur.execute(
                        "UPDATE ghost_links SET ref_count = ref_count - 1 WHERE dst_name = ?",
                        (name,),
                    )

                # 6. Apply new ghost-link counts
                now = time.time()
                for e in edges:
                    if e.dst_doc_id is None:
                        cur.execute(
                            """
                            INSERT INTO ghost_links (dst_name, ref_count, first_seen)
                            VALUES (?, 1, ?)
                            ON CONFLICT(dst_name) DO UPDATE SET
                                ref_count = ref_count + 1
                            """,
                            (e.dst_name, now),
                        )

                # 7. Drop ghost rows that hit zero
                cur.execute("DELETE FROM ghost_links WHERE ref_count <= 0")

                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    def delete_doc(self, doc_id: str) -> None:
        """Remove a doc, its edges, and decrement ghost-link counts.

        Used when a vault file is deleted.
        """
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                old_ghosts = [
                    r["dst_name"]
                    for r in cur.execute(
                        "SELECT dst_name FROM edges WHERE src_doc_id = ? AND dst_doc_id IS NULL",
                        (doc_id,),
                    ).fetchall()
                ]
                cur.execute("DELETE FROM edges WHERE src_doc_id = ?", (doc_id,))
                cur.execute("DELETE FROM docs_fts WHERE doc_id = ?", (doc_id,))
                cur.execute("DELETE FROM docs WHERE doc_id = ?", (doc_id,))
                for name in old_ghosts:
                    cur.execute(
                        "UPDATE ghost_links SET ref_count = ref_count - 1 WHERE dst_name = ?",
                        (name,),
                    )
                cur.execute("DELETE FROM ghost_links WHERE ref_count <= 0")
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

    # ---------- resolution helpers ----------

    def all_filenames(self) -> List[Tuple[str, str]]:
        """Return [(filename_lower_no_ext, doc_id), ...] for filename resolution.

        Caller (the indexer) builds an in-memory dict from this for O(1) lookups.
        """
        out: List[Tuple[str, str]] = []
        with self._lock:
            for r in self._conn.execute("SELECT doc_id FROM docs"):
                doc_id: str = r["doc_id"]
                # basename without .md extension, lowercased
                base = doc_id.rsplit("/", 1)[-1]
                if base.lower().endswith(".md"):
                    base = base[:-3]
                out.append((base.lower(), doc_id))
        return out

    def list_doc_ids(self) -> List[str]:
        with self._lock:
            return [
                r["doc_id"] for r in self._conn.execute("SELECT doc_id FROM docs ORDER BY doc_id")
            ]

    def all_docs(self) -> List[DocRow]:
        with self._lock:
            return [
                _row_to_doc(r)
                for r in self._conn.execute("SELECT * FROM docs ORDER BY doc_id")
            ]

    def all_aliases(self) -> List[Tuple[str, str]]:
        """Return [(alias_lower, doc_id), ...] for alias-based resolution."""
        out: List[Tuple[str, str]] = []
        with self._lock:
            for r in self._conn.execute("SELECT doc_id, aliases FROM docs"):
                try:
                    aliases = json.loads(r["aliases"] or "[]")
                except json.JSONDecodeError:
                    continue
                for a in aliases:
                    if isinstance(a, str):
                        out.append((a.lower(), r["doc_id"]))
        return out

    def all_titles(self) -> List[Tuple[str, str]]:
        """Return [(title_lower, doc_id), ...] for title-based resolution."""
        with self._lock:
            return [
                (r["title"].lower(), r["doc_id"])
                for r in self._conn.execute(
                    "SELECT doc_id, title FROM docs WHERE title IS NOT NULL AND title != ''"
                )
            ]

    # ---------- counts ----------

    def doc_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS c FROM docs").fetchone()["c"]

    def edge_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]

    def ghost_link_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS c FROM ghost_links"
            ).fetchone()["c"]

    # ---------- FTS ----------

    def fts_search(self, query: str, limit: int = 20) -> List[Tuple[str, str]]:
        """Return [(doc_id, title), ...] ranked by FTS5 bm25.

        bm25() is a function on the FTS table itself; it must be called with
        the FTS table name (not a JOIN alias). We therefore do the ranking in
        a subquery and JOIN to docs to fetch the title.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                WITH ranked AS (
                    SELECT doc_id, bm25(docs_fts) AS rank
                    FROM docs_fts
                    WHERE docs_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                )
                SELECT d.doc_id, d.title
                FROM ranked r
                JOIN docs d ON d.doc_id = r.doc_id
                ORDER BY r.rank
                """,
                (query, limit),
            ).fetchall()
        return [(r["doc_id"], r["title"]) for r in rows]


# ---------- helpers ----------

def _row_to_doc(row: sqlite3.Row) -> DocRow:
    try:
        aliases = json.loads(row["aliases"] or "[]")
    except (json.JSONDecodeError, TypeError):
        aliases = []
    return DocRow(
        doc_id=row["doc_id"],
        abs_path=row["abs_path"],
        type=row["type"],
        status=row["status"],
        title=row["title"],
        aliases=aliases,
        body_text=row["body_text"] or "",
        hash=row["hash"],
        mtime=row["mtime"],
        indexed_at=row["indexed_at"],
        external_source=row["external_source"],
        external_kind=row["external_kind"],
        is_reference=bool(row["is_reference"]),
    )
