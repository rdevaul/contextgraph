"""Unit tests for doc_indexer.py — frontmatter parse, edge extraction, resolver,
ghost-link plumbing, cold-start idempotency, watch-mode update."""

import sys
import time
from pathlib import Path
from textwrap import dedent

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from doc_store import DocStore  # noqa: E402
from doc_indexer import (  # noqa: E402
    DocIndexer,
    Resolver,
    extract_aliases,
    extract_edges,
    extract_wikilinks_from_value,
    parse_doc,
    split_frontmatter,
    _preprocess_tolaria_yaml,
)


# ---------- frontmatter split ----------

def test_split_frontmatter_present():
    content = "---\ntype: Spec\n---\n# Title\nbody"
    fm, body = split_frontmatter(content)
    assert fm == "type: Spec"
    assert body == "# Title\nbody"


def test_split_frontmatter_absent():
    content = "# Title\nno frontmatter here"
    fm, body = split_frontmatter(content)
    assert fm is None
    assert body == content


def test_split_frontmatter_unterminated_treats_as_body():
    content = "---\ntype: Spec\nno closing"
    fm, body = split_frontmatter(content)
    assert fm is None
    assert body == content


# ---------- tolaria yaml preprocessor ----------

def test_preprocess_bare_wikilinks():
    raw = "related_to: [[foo]] [[bar]]\nstatus: draft\n"
    out = _preprocess_tolaria_yaml(raw)
    # Should turn the bare wikilink line into a quoted YAML list
    assert "related_to: ['[[foo]]', '[[bar]]']" in out
    # Other lines untouched
    assert "status: draft" in out


def test_preprocess_leaves_normal_yaml_alone():
    raw = "key: value\nlist:\n  - a\n  - b\n"
    assert _preprocess_tolaria_yaml(raw) == raw


def test_preprocess_handles_single_wikilink():
    raw = "supersedes: [[old-thing]]\n"
    out = _preprocess_tolaria_yaml(raw)
    assert "supersedes: ['[[old-thing]]']" in out
    # And the result must parse cleanly as YAML
    import yaml
    parsed = yaml.safe_load(out)
    assert parsed == {"supersedes": ["[[old-thing]]"]}


# ---------- parse_doc ----------

def test_parse_doc_extracts_title():
    parsed = parse_doc("# Hello World\nbody")
    assert parsed.title == "Hello World"
    assert parsed.frontmatter == {}


def test_parse_doc_handles_tolaria_frontmatter():
    content = dedent("""\
        ---
        type: Proposal
        status: draft
        related_to: [[foo]] [[bar]]
        ---
        # Doc Title
        Body.
    """)
    parsed = parse_doc(content)
    assert parsed.frontmatter["type"] == "Proposal"
    assert parsed.frontmatter["related_to"] == ["[[foo]]", "[[bar]]"]
    assert parsed.title == "Doc Title"
    assert parsed.parse_warning is None


def test_parse_doc_malformed_yaml_warns_but_continues():
    content = "---\nthis is: : not valid: yaml:\n---\n# Title\nbody"
    parsed = parse_doc(content)
    assert parsed.parse_warning is not None
    # Body and title still extracted
    assert parsed.title == "Title"


def test_parse_doc_empty_frontmatter():
    parsed = parse_doc("---\n---\n# T\n")
    assert parsed.frontmatter == {}
    assert parsed.parse_warning is None


# ---------- extract_wikilinks ----------

def test_wikilinks_from_string():
    assert extract_wikilinks_from_value("[[a]] [[b]]") == ["a", "b"]


def test_wikilinks_dedup_preserve_order():
    assert extract_wikilinks_from_value("[[a]] [[b]] [[a]]") == ["a", "b"]


def test_wikilinks_alias_form():
    assert extract_wikilinks_from_value("[[Tank|Oxidizer Tank]]") == ["Tank"]


def test_wikilinks_from_list():
    assert extract_wikilinks_from_value(["[[a]]", "[[b]]", "[[c]]"]) == ["a", "b", "c"]


def test_wikilinks_from_none():
    assert extract_wikilinks_from_value(None) == []


def test_wikilinks_from_int():
    assert extract_wikilinks_from_value(42) == []


# ---------- extract_edges ----------

def test_extract_edges_skips_system_fields():
    fm = {
        "_external_source": "[[ignore-me]]",
        "_indexed_at": 12345.0,
        "related_to": ["[[a]]"],
    }
    edges = extract_edges(fm)
    assert edges == [("related_to", "a")]


def test_extract_edges_skips_metadata_fields():
    fm = {
        "type": "Proposal",
        "status": "draft",
        "title": "[[not an edge]]",  # title is metadata, not a relationship
        "author": "[[also not]]",
        "related_to": ["[[real-edge]]"],
    }
    edges = extract_edges(fm)
    assert edges == [("related_to", "real-edge")]


def test_extract_edges_novel_rel_type():
    fm = {"mates_to": ["[[fin-1]]"]}
    edges = extract_edges(fm)
    assert edges == [("mates_to", "fin-1")]


# ---------- aliases ----------

def test_aliases_list():
    assert extract_aliases({"aliases": ["A", "B"]}) == ["A", "B"]


def test_aliases_scalar():
    assert extract_aliases({"aliases": "A"}) == ["A"]


def test_aliases_missing():
    assert extract_aliases({}) == []


# ---------- resolver ----------

def test_resolver_filename_match(tmp_path):
    s = DocStore(":memory:")
    try:
        from doc_store import EdgeRow
        from doc_indexer import DocRow as IDocRow  # same DocRow
        s.upsert_doc_with_edges(
            IDocRow(
                doc_id="projects/foo.md", abs_path="/x", title="Foo Spec",
                aliases=["Foo Alias"], hash="h", mtime=0, indexed_at=0,
            ),
            [],
        )
        r = Resolver.from_store(s)
        # filename match (case-insensitive, .md optional)
        assert r.resolve("foo") == ("projects/foo.md", False)
        assert r.resolve("FOO") == ("projects/foo.md", False)
        assert r.resolve("foo.md") == ("projects/foo.md", False)
        # alias match
        assert r.resolve("foo alias") == ("projects/foo.md", False)
        # title match
        assert r.resolve("Foo Spec") == ("projects/foo.md", False)
        # ghost
        assert r.resolve("nonexistent") == (None, False)
    finally:
        s.close()


def test_resolver_ambiguous_returns_alphabetical_first():
    s = DocStore(":memory:")
    try:
        from doc_indexer import DocRow as IDocRow
        s.upsert_doc_with_edges(
            IDocRow(doc_id="z/dup.md", abs_path="/x", hash="h1", mtime=0, indexed_at=0), []
        )
        s.upsert_doc_with_edges(
            IDocRow(doc_id="a/dup.md", abs_path="/x", hash="h2", mtime=0, indexed_at=0), []
        )
        r = Resolver.from_store(s)
        result, ambiguous = r.resolve("dup")
        assert ambiguous is True
        assert result == "a/dup.md"  # alphabetical
    finally:
        s.close()


# ---------- end-to-end indexer ----------

def _make_vault(tmp_path: Path, files: dict) -> Path:
    """Create a fixture vault. `files` maps relative path -> content."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for rel, content in files.items():
        full = vault / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return vault


def test_cold_start_indexes_all_docs(tmp_path):
    vault = _make_vault(tmp_path, {
        "a.md": "# A\nbody a\n",
        "sub/b.md": "# B\nbody b\n",
        "sub/c.md": "# C\nbody c\n",
    })
    s = DocStore(tmp_path / "db.sqlite")
    try:
        idx = DocIndexer(s, vault)
        stats = idx.cold_start()
        assert stats["inserted"] == 3
        assert s.doc_count() == 3
        assert s.edge_count() == 0
        # FTS populated
        hits = s.fts_search("body")
        assert len(hits) == 3
    finally:
        s.close()


def test_cold_start_idempotent(tmp_path):
    vault = _make_vault(tmp_path, {"a.md": "# A\n"})
    s = DocStore(tmp_path / "db.sqlite")
    try:
        idx = DocIndexer(s, vault)
        idx.cold_start()
        # Second pass: no writes
        stats = idx.cold_start()
        assert stats["inserted"] == 0
        assert stats["updated"] == 0
        assert stats["skipped"] == 1
    finally:
        s.close()


def test_ghost_link_plumbing(tmp_path):
    """Add a doc with belongs_to: [[nonexistent]], verify ghost_links has it."""
    vault = _make_vault(tmp_path, {
        "child.md": dedent("""\
            ---
            type: WBS
            belongs_to: [[nonexistent]]
            ---
            # Child
        """),
    })
    s = DocStore(tmp_path / "db.sqlite")
    try:
        idx = DocIndexer(s, vault)
        idx.cold_start()
        assert s.ghost_link_count() == 1
        row = s._conn.execute(
            "SELECT * FROM ghost_links WHERE dst_name='nonexistent'"
        ).fetchone()
        assert row is not None
        assert row["ref_count"] == 1
        # Edge recorded with NULL dst_doc_id
        edge = s._conn.execute(
            "SELECT * FROM edges WHERE src_doc_id='child.md' AND rel_type='belongs_to'"
        ).fetchone()
        assert edge is not None
        assert edge["dst_name"] == "nonexistent"
        assert edge["dst_doc_id"] is None
    finally:
        s.close()


def test_ghost_promotes_when_target_appears(tmp_path):
    """Ghost link → resolved when the target file appears in the vault."""
    vault = _make_vault(tmp_path, {
        "child.md": "---\nbelongs_to: [[parent]]\n---\n# Child\n",
    })
    s = DocStore(tmp_path / "db.sqlite")
    try:
        idx = DocIndexer(s, vault)
        idx.cold_start()
        assert s.ghost_link_count() == 1

        # Now create the target and re-index
        (vault / "parent.md").write_text("# Parent\n", encoding="utf-8")
        idx.cold_start()

        assert s.ghost_link_count() == 0
        edge = s._conn.execute(
            "SELECT * FROM edges WHERE src_doc_id='child.md'"
        ).fetchone()
        assert edge["dst_doc_id"] == "parent.md"
    finally:
        s.close()


def test_indexer_handles_malformed_frontmatter(tmp_path):
    """Malformed YAML → warning + body still indexed for FTS."""
    vault = _make_vault(tmp_path, {
        "bad.md": "---\nthis: : is: not valid: :\n---\n# Bad Doc\nsearchable body\n",
    })
    s = DocStore(tmp_path / "db.sqlite")
    try:
        idx = DocIndexer(s, vault)
        idx.cold_start()
        assert s.doc_count() == 1
        # Body should still be searchable
        assert ("bad.md", "Bad Doc") in s.fts_search("searchable")
    finally:
        s.close()


def test_indexer_resolves_known_doc(tmp_path):
    """[[foo]] → projects/foo.md when that file exists."""
    vault = _make_vault(tmp_path, {
        "projects/foo.md": "# Foo\n",
        "ref.md": "---\nrelated_to: [[foo]]\n---\n# Ref\n",
    })
    s = DocStore(tmp_path / "db.sqlite")
    try:
        idx = DocIndexer(s, vault)
        idx.cold_start()
        edge = s._conn.execute(
            "SELECT dst_doc_id FROM edges WHERE src_doc_id='ref.md'"
        ).fetchone()
        assert edge["dst_doc_id"] == "projects/foo.md"
        assert s.ghost_link_count() == 0
    finally:
        s.close()


def test_indexer_external_source_promotes_to_columns(tmp_path):
    """_external_source/_external_kind end up on the doc row."""
    vault = _make_vault(tmp_path, {
        "ref.md": dedent("""\
            ---
            type: Reference
            _external_source: /tmp/orig.pdf
            _external_kind: pdf
            ---
            # External
        """),
    })
    s = DocStore(tmp_path / "db.sqlite")
    try:
        idx = DocIndexer(s, vault)
        idx.cold_start()
        row = s._conn.execute(
            "SELECT external_source, external_kind, is_reference FROM docs WHERE doc_id='ref.md'"
        ).fetchone()
        assert row["external_source"] == "/tmp/orig.pdf"
        assert row["external_kind"] == "pdf"
        assert row["is_reference"] == 1
    finally:
        s.close()


def test_indexer_hash_skip(tmp_path):
    """Re-indexing an unchanged file does no DB writes."""
    vault = _make_vault(tmp_path, {"a.md": "# A\n"})
    s = DocStore(tmp_path / "db.sqlite")
    try:
        idx = DocIndexer(s, vault)
        idx.cold_start()
        first_indexed = s.get_doc("a.md").indexed_at

        time.sleep(0.05)
        stats = idx.cold_start()
        assert stats["skipped"] == 1
        assert stats["updated"] == 0
        # indexed_at unchanged because we didn't actually re-write
        assert s.get_doc("a.md").indexed_at == first_indexed
    finally:
        s.close()


def test_indexer_change_triggers_reindex(tmp_path):
    """A modified file IS re-indexed (hash changes)."""
    vault = _make_vault(tmp_path, {"a.md": "# A\noriginal text\n"})
    s = DocStore(tmp_path / "db.sqlite")
    try:
        idx = DocIndexer(s, vault)
        idx.cold_start()
        first_hash = s.get_doc("a.md").hash

        (vault / "a.md").write_text("# A\nreplacement word\n", encoding="utf-8")
        stats = idx.cold_start()
        assert stats["updated"] == 1
        assert s.get_doc("a.md").hash != first_hash
        # FTS reflects new content (and old content is gone)
        assert ("a.md", "A") in s.fts_search("replacement")
        assert s.fts_search("original") == []
    finally:
        s.close()


def test_doc_id_excludes_system_fields_from_edges():
    """Frontmatter `_external_source: [[wont-resolve]]` MUST NOT create an edge."""
    fm = {
        "_external_source": "[[shouldnt-be-edge]]",
        "related_to": ["[[real-edge]]"],
    }
    edges = extract_edges(fm)
    rel_types = {r for r, _ in edges}
    assert "_external_source" not in rel_types
    assert ("related_to", "real-edge") in edges
