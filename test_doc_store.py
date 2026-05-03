"""
test_doc_store.py — unit tests for the SQLite layer.

Run:
    cd ~/Projects/contextgraph && python -m pytest test_doc_store.py -v
"""

from __future__ import annotations

import json
import time

import pytest

from doc_store import DocRow, DocStore, EdgeRow


# ---------- schema ----------

def test_schema_creates_all_tables(tmp_path):
    db = tmp_path / "x.db"
    store = DocStore(db)
    try:
        names = [
            r["name"] for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
            )
        ]
    finally:
        store.close()
    # docs_fts is FTS5 — sqlite exposes shadow tables, so check the virtual table itself.
    assert "docs" in names
    assert "edges" in names
    assert "ghost_links" in names
    assert "schema_meta" in names
    assert "docs_fts" in names


def test_schema_version_recorded(tmp_path):
    store = DocStore(tmp_path / "x.db")
    try:
        row = store._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert row is not None
        assert int(row["value"]) >= 1
    finally:
        store.close()


def test_init_is_idempotent(tmp_path):
    """Opening the same DB twice must not error or duplicate schema rows."""
    db = tmp_path / "x.db"
    s1 = DocStore(db)
    s1.close()
    s2 = DocStore(db)
    try:
        # schema_meta still has exactly one schema_version row
        rows = s2._conn.execute(
            "SELECT COUNT(*) AS c FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert rows["c"] == 1
    finally:
        s2.close()


# ---------- doc upsert ----------

def _doc(doc_id: str, **kw) -> DocRow:
    defaults = dict(
        abs_path=f"/tmp/{doc_id}",
        title=doc_id.rsplit("/", 1)[-1].replace(".md", "").title(),
        body_text="hello world",
        hash="h" + doc_id,
        mtime=1000.0,
        indexed_at=time.time(),
    )
    defaults.update(kw)
    return DocRow(doc_id=doc_id, **defaults)


def test_upsert_inserts_then_returns_doc(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(_doc("a.md", aliases=["alpha", "first"]), [])
        got = s.get_doc("a.md")
        assert got is not None
        assert got.doc_id == "a.md"
        assert got.aliases == ["alpha", "first"]
        assert got.body_text == "hello world"
    finally:
        s.close()


def test_upsert_updates_existing(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(_doc("a.md", title="v1", hash="h1"), [])
        s.upsert_doc_with_edges(_doc("a.md", title="v2", hash="h2", body_text="new body"), [])
        got = s.get_doc("a.md")
        assert got.title == "v2"
        assert got.hash == "h2"
        assert got.body_text == "new body"
        # Still exactly one row
        assert s.doc_count() == 1
    finally:
        s.close()


def test_get_doc_hash_fast_path(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        assert s.get_doc_hash("missing.md") is None
        s.upsert_doc_with_edges(_doc("a.md", hash="abc123"), [])
        assert s.get_doc_hash("a.md") == "abc123"
    finally:
        s.close()


# ---------- edges + ghosts ----------

def test_edges_with_resolved_target_no_ghost(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(_doc("target.md"), [])
        s.upsert_doc_with_edges(
            _doc("src.md"),
            [EdgeRow(src_doc_id="src.md", rel_type="related_to",
                     dst_name="target", dst_doc_id="target.md")],
        )
        assert s.edge_count() == 1
        assert s.ghost_link_count() == 0
    finally:
        s.close()


def test_edges_with_unresolved_target_creates_ghost(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(
            _doc("src.md"),
            [EdgeRow("src.md", "related_to", "ghost-thing", None)],
        )
        assert s.edge_count() == 1
        assert s.ghost_link_count() == 1
        row = s._conn.execute(
            "SELECT ref_count FROM ghost_links WHERE dst_name = 'ghost-thing'"
        ).fetchone()
        assert row["ref_count"] == 1
    finally:
        s.close()


def test_ghost_refcount_aggregates_across_sources(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(
            _doc("a.md"),
            [EdgeRow("a.md", "related_to", "ghost", None)],
        )
        s.upsert_doc_with_edges(
            _doc("b.md"),
            [EdgeRow("b.md", "related_to", "ghost", None)],
        )
        row = s._conn.execute(
            "SELECT ref_count FROM ghost_links WHERE dst_name = 'ghost'"
        ).fetchone()
        assert row["ref_count"] == 2
    finally:
        s.close()


def test_reupsert_replaces_edges_and_decrements_old_ghost(tmp_path):
    """Removing a ghost edge in a re-upsert must decrement the ghost ref_count
    so stale ghosts don't persist forever."""
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(
            _doc("a.md"),
            [EdgeRow("a.md", "related_to", "ghost-x", None)],
        )
        assert s.ghost_link_count() == 1
        # Re-upsert with NO edges → ghost-x ref_count goes to 0 → row gone
        s.upsert_doc_with_edges(_doc("a.md", hash="h2"), [])
        assert s.ghost_link_count() == 0
        assert s.edge_count() == 0
    finally:
        s.close()


def test_reupsert_swaps_ghost_to_resolved(tmp_path):
    """When the target doc is later created, a re-upsert of the source should
    flip the edge from ghost to resolved and the ghost row should disappear."""
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(
            _doc("a.md"),
            [EdgeRow("a.md", "related_to", "target", None)],
        )
        assert s.ghost_link_count() == 1
        # Now target.md exists, re-upsert a.md with a resolved edge
        s.upsert_doc_with_edges(_doc("target.md"), [])
        s.upsert_doc_with_edges(
            _doc("a.md", hash="h2"),
            [EdgeRow("a.md", "related_to", "target", "target.md")],
        )
        assert s.edge_count() == 1
        assert s.ghost_link_count() == 0
        # And the surviving edge points to the resolved doc
        row = s._conn.execute(
            "SELECT dst_doc_id FROM edges WHERE src_doc_id = 'a.md'"
        ).fetchone()
        assert row["dst_doc_id"] == "target.md"
    finally:
        s.close()


def test_delete_doc_cascades_edges_and_ghosts(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(
            _doc("a.md"),
            [
                EdgeRow("a.md", "related_to", "ghost-1", None),
                EdgeRow("a.md", "related_to", "ghost-2", None),
            ],
        )
        assert s.ghost_link_count() == 2
        s.delete_doc("a.md")
        assert s.get_doc("a.md") is None
        assert s.edge_count() == 0
        assert s.ghost_link_count() == 0
    finally:
        s.close()


def test_delete_doc_only_decrements_shared_ghost(tmp_path):
    """If two sources reference the same ghost and one source is deleted,
    the ghost ref_count drops by 1 but the row stays."""
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(
            _doc("a.md"),
            [EdgeRow("a.md", "related_to", "shared", None)],
        )
        s.upsert_doc_with_edges(
            _doc("b.md"),
            [EdgeRow("b.md", "related_to", "shared", None)],
        )
        s.delete_doc("a.md")
        assert s.ghost_link_count() == 1
        row = s._conn.execute(
            "SELECT ref_count FROM ghost_links WHERE dst_name = 'shared'"
        ).fetchone()
        assert row["ref_count"] == 1
    finally:
        s.close()


def test_duplicate_edge_in_one_upsert_dedupes_via_pk(tmp_path):
    """The PK is (src, rel_type, dst_name). Two identical edges in the same
    upsert should collapse to one row (INSERT OR IGNORE behavior)."""
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(
            _doc("a.md"),
            [
                EdgeRow("a.md", "related_to", "x", None),
                EdgeRow("a.md", "related_to", "x", None),
            ],
        )
        assert s.edge_count() == 1
    finally:
        s.close()


# ---------- resolution helpers ----------

def test_all_filenames_strips_md_and_lowercases(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(_doc("Projects/Agentic-1.md"), [])
        names = dict(s.all_filenames())
        assert "agentic-1" in names
        assert names["agentic-1"] == "Projects/Agentic-1.md"
    finally:
        s.close()


def test_all_aliases_lowercased_and_dedupes_per_doc(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(_doc("a.md", aliases=["Alpha", "FIRST"]), [])
        out = dict(s.all_aliases())
        assert "alpha" in out and "first" in out
        assert out["alpha"] == "a.md"
    finally:
        s.close()


def test_all_titles_lowercased_skips_empty(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(_doc("a.md", title="My Title"), [])
        s.upsert_doc_with_edges(_doc("b.md", title=""), [])  # filtered out
        s.upsert_doc_with_edges(_doc("c.md", title=None), [])  # filtered out
        out = dict(s.all_titles())
        assert "my title" in out
        assert "" not in out
    finally:
        s.close()


# ---------- FTS ----------

def test_fts_returns_hits(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(
            _doc("a.md", title="Forward Bulkhead", body_text="bolt circle pattern"),
            [],
        )
        s.upsert_doc_with_edges(
            _doc("b.md", title="Nosecone", body_text="aerodynamic ogive"),
            [],
        )
        hits = s.fts_search("bolt")
        assert len(hits) == 1
        assert hits[0][0] == "a.md"
    finally:
        s.close()


def test_fts_updated_on_reupsert(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(
            _doc("a.md", body_text="alpha"), [],
        )
        assert len(s.fts_search("alpha")) == 1
        s.upsert_doc_with_edges(
            _doc("a.md", hash="h2", body_text="bravo"), [],
        )
        # Old term gone, new term hits
        assert len(s.fts_search("alpha")) == 0
        assert len(s.fts_search("bravo")) == 1
    finally:
        s.close()


def test_fts_dropped_on_delete(tmp_path):
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(_doc("a.md", body_text="findme"), [])
        assert len(s.fts_search("findme")) == 1
        s.delete_doc("a.md")
        assert len(s.fts_search("findme")) == 0
    finally:
        s.close()


# ---------- atomicity ----------

def test_upsert_rollback_leaves_store_clean(tmp_path):
    """If the body of upsert raises, we must ROLLBACK and not leave a half-doc.

    Force the failure by passing a value sqlite3 can't bind (a dict).
    """
    s = DocStore(tmp_path / "x.db")
    try:
        s.upsert_doc_with_edges(_doc("a.md", title="original"), [])
        # Bind error: rel_type as a dict will fail sqlite3 binding mid-transaction
        bad = EdgeRow(src_doc_id="a.md", rel_type={"not": "a string"},  # type: ignore
                      dst_name="x", dst_doc_id=None)
        with pytest.raises(Exception):
            s.upsert_doc_with_edges(_doc("a.md", hash="h2", title="willnotstick"), [bad])
        # Original row preserved (rollback worked)
        got = s.get_doc("a.md")
        assert got is not None
        assert got.title == "original"
        assert got.hash == "ha.md"  # original hash, not h2
    finally:
        s.close()
