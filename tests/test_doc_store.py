"""Unit tests for doc_store.py — schema, CRUD, FTS, ghost-link bookkeeping."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from doc_store import DocRow, DocStore, EdgeRow  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = DocStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def in_memory_store():
    s = DocStore(":memory:")
    yield s
    s.close()


# ---------- schema ----------

def test_schema_creates_all_tables(store):
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {r["name"] for r in cur.fetchall()}
    assert {"docs", "edges", "ghost_links", "schema_meta", "docs_fts"}.issubset(tables)


def test_schema_version_recorded(store):
    row = store._conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    assert row["value"] == "1"


# ---------- doc upsert ----------

def _doc(doc_id: str, **kw) -> DocRow:
    base = dict(
        doc_id=doc_id,
        abs_path=f"/tmp/{doc_id}",
        type=None, status=None, title=None,
        aliases=[], body_text="", hash="h" + doc_id, mtime=0.0,
        indexed_at=1.0, external_source=None, external_kind=None,
        is_reference=False,
    )
    base.update(kw)
    return DocRow(**base)


def test_insert_doc_no_edges(in_memory_store):
    s = in_memory_store
    s.upsert_doc_with_edges(_doc("a.md", title="A"), [])
    assert s.doc_count() == 1
    assert s.edge_count() == 0
    assert s.ghost_link_count() == 0
    got = s.get_doc("a.md")
    assert got is not None
    assert got.title == "A"


def test_insert_doc_with_resolved_edge(in_memory_store):
    s = in_memory_store
    s.upsert_doc_with_edges(_doc("target.md", title="Target"), [])
    s.upsert_doc_with_edges(
        _doc("src.md"),
        [EdgeRow("src.md", "related_to", "target", "target.md")],
    )
    assert s.edge_count() == 1
    assert s.ghost_link_count() == 0


def test_ghost_link_creation(in_memory_store):
    s = in_memory_store
    s.upsert_doc_with_edges(
        _doc("src.md"),
        [EdgeRow("src.md", "belongs_to", "ghost", None)],
    )
    assert s.ghost_link_count() == 1
    row = s._conn.execute(
        "SELECT * FROM ghost_links WHERE dst_name='ghost'"
    ).fetchone()
    assert row["ref_count"] == 1


def test_ghost_link_ref_count_increments(in_memory_store):
    s = in_memory_store
    s.upsert_doc_with_edges(
        _doc("a.md"), [EdgeRow("a.md", "rel", "ghost", None)]
    )
    s.upsert_doc_with_edges(
        _doc("b.md"), [EdgeRow("b.md", "rel", "ghost", None)]
    )
    row = s._conn.execute(
        "SELECT ref_count FROM ghost_links WHERE dst_name='ghost'"
    ).fetchone()
    assert row["ref_count"] == 2


def test_upsert_replaces_old_edges(in_memory_store):
    s = in_memory_store
    # First version: 2 ghost edges
    s.upsert_doc_with_edges(
        _doc("a.md"),
        [
            EdgeRow("a.md", "rel", "g1", None),
            EdgeRow("a.md", "rel", "g2", None),
        ],
    )
    assert s.ghost_link_count() == 2
    # Updated version: only 1 edge, different target
    s.upsert_doc_with_edges(
        _doc("a.md", hash="h2"),
        [EdgeRow("a.md", "rel", "g3", None)],
    )
    assert s.edge_count() == 1
    # g1 and g2 should now have ref_count=0 and be cleaned up; g3 should be 1
    rows = s._conn.execute("SELECT dst_name, ref_count FROM ghost_links").fetchall()
    names = {r["dst_name"]: r["ref_count"] for r in rows}
    assert names == {"g3": 1}


def test_delete_doc_decrements_ghosts(in_memory_store):
    s = in_memory_store
    s.upsert_doc_with_edges(
        _doc("a.md"), [EdgeRow("a.md", "rel", "ghost", None)]
    )
    s.upsert_doc_with_edges(
        _doc("b.md"), [EdgeRow("b.md", "rel", "ghost", None)]
    )
    assert s.ghost_link_count() == 1  # both pointing to same name
    s.delete_doc("a.md")
    row = s._conn.execute(
        "SELECT ref_count FROM ghost_links WHERE dst_name='ghost'"
    ).fetchone()
    assert row["ref_count"] == 1
    s.delete_doc("b.md")
    assert s.ghost_link_count() == 0


def test_get_doc_hash_fast_path(in_memory_store):
    s = in_memory_store
    assert s.get_doc_hash("nope.md") is None
    s.upsert_doc_with_edges(_doc("a.md", hash="abc"), [])
    assert s.get_doc_hash("a.md") == "abc"


# ---------- resolution helpers ----------

def test_all_filenames_excludes_md_extension(in_memory_store):
    s = in_memory_store
    s.upsert_doc_with_edges(_doc("projects/foo.md"), [])
    pairs = dict(s.all_filenames())
    assert pairs == {"foo": "projects/foo.md"}


def test_all_aliases_lowercased(in_memory_store):
    s = in_memory_store
    s.upsert_doc_with_edges(_doc("a.md", aliases=["Tank-V3", "TANK"]), [])
    pairs = s.all_aliases()
    assert ("tank-v3", "a.md") in pairs
    assert ("tank", "a.md") in pairs


def test_all_titles_excludes_empty(in_memory_store):
    s = in_memory_store
    s.upsert_doc_with_edges(_doc("a.md", title="Hello"), [])
    s.upsert_doc_with_edges(_doc("b.md", title=None), [])
    s.upsert_doc_with_edges(_doc("c.md", title=""), [])
    pairs = s.all_titles()
    assert pairs == [("hello", "a.md")]


# ---------- FTS ----------

def test_fts_returns_hits(in_memory_store):
    s = in_memory_store
    s.upsert_doc_with_edges(
        _doc("rocket.md", title="Rocket Spec", body_text="oxidizer tank pressure test"), []
    )
    s.upsert_doc_with_edges(
        _doc("other.md", title="Other", body_text="something completely unrelated"), []
    )
    hits = s.fts_search("oxidizer", limit=10)
    assert any(doc_id == "rocket.md" for doc_id, _ in hits)


def test_fts_updates_on_reindex(in_memory_store):
    s = in_memory_store
    s.upsert_doc_with_edges(_doc("a.md", title="Alpha", body_text="initial"), [])
    s.upsert_doc_with_edges(
        _doc("a.md", title="Alpha", body_text="changed", hash="h2"), []
    )
    assert s.fts_search("initial") == []
    assert ("a.md", "Alpha") in s.fts_search("changed")


# ---------- is_reference flag ----------

def test_is_reference_flag(in_memory_store):
    s = in_memory_store
    s.upsert_doc_with_edges(
        _doc("ref.md", type="Reference", external_source="/tmp/orig.pdf",
             external_kind="pdf", is_reference=True),
        [],
    )
    row = s._conn.execute(
        "SELECT is_reference, external_source, external_kind FROM docs WHERE doc_id='ref.md'"
    ).fetchone()
    assert row["is_reference"] == 1
    assert row["external_source"] == "/tmp/orig.pdf"
    assert row["external_kind"] == "pdf"
