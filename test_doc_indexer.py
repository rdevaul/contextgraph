"""
test_doc_indexer.py — unit tests for the markdown indexer.

Run:
    cd ~/Projects/contextgraph && python -m pytest test_doc_indexer.py -v
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from doc_indexer import (
    DocIndexer,
    Resolver,
    extract_aliases,
    extract_edges,
    extract_wikilinks_from_value,
    parse_doc,
    split_frontmatter,
    _preprocess_tolaria_yaml,
)
from doc_store import DocStore


# ---------- frontmatter parsing ----------

def test_split_no_frontmatter():
    fm, body = split_frontmatter("# Just a doc\n\nhello")
    assert fm is None
    assert body.startswith("# Just a doc")


def test_split_basic_frontmatter():
    text = "---\ntitle: Foo\n---\nbody here"
    fm, body = split_frontmatter(text)
    assert fm == "title: Foo"
    assert body == "body here"


def test_split_unterminated_frontmatter_treated_as_no_fm():
    text = "---\ntitle: Foo\nno closer\nstill body\n"
    fm, body = split_frontmatter(text)
    assert fm is None
    assert "title: Foo" in body  # left intact


def test_parse_doc_extracts_h1_title():
    text = "---\ntype: Project\n---\n# My Project\n\nbody"
    parsed = parse_doc(text)
    assert parsed.title == "My Project"
    assert parsed.frontmatter == {"type": "Project"}
    assert parsed.parse_warning is None


def test_parse_doc_no_h1_no_title():
    text = "---\ntype: Project\n---\n\nbody only, no header"
    parsed = parse_doc(text)
    assert parsed.title is None


def test_parse_doc_malformed_yaml_warns_and_continues():
    # Truly malformed (unbalanced quote, no wikilinks rescue path)
    text = '---\ntitle: "unterminated\nbroken: yes\n---\n# Title\nbody'
    parsed = parse_doc(text)
    # We should still get a body and a title
    assert parsed.title == "Title"
    assert parsed.parse_warning is not None


# ---------- Tolaria-style YAML preprocessing ----------

def test_tolaria_preprocess_single_bare_link():
    src = "related_to: [[foo]]"
    out = _preprocess_tolaria_yaml(src)
    # Should produce a valid YAML flow list
    import yaml
    loaded = yaml.safe_load(out)
    assert loaded == {"related_to": ["[[foo]]"]}


def test_tolaria_preprocess_multiple_bare_links_one_line():
    src = "related_to: [[a]] [[b]] [[c]]"
    out = _preprocess_tolaria_yaml(src)
    import yaml
    loaded = yaml.safe_load(out)
    assert loaded == {"related_to": ["[[a]]", "[[b]]", "[[c]]"]}


def test_tolaria_preprocess_leaves_normal_yaml_alone():
    src = "title: Foo\ntype: Project"
    out = _preprocess_tolaria_yaml(src)
    assert out == src


def test_parse_doc_handles_bare_wikilink_frontmatter_no_warning():
    """The actual symptom from the wikilink-relationships proposal vault."""
    text = (
        "---\n"
        "type: Proposal\n"
        "related_to: [[contextgraph]] [[whiteboard]]\n"
        "inspired_by: [[tolaria]]\n"
        "---\n"
        "# Wikilink Relationships\nbody"
    )
    parsed = parse_doc(text)
    assert parsed.parse_warning is None
    assert parsed.frontmatter["related_to"] == ["[[contextgraph]]", "[[whiteboard]]"]
    assert parsed.frontmatter["inspired_by"] == ["[[tolaria]]"]


# ---------- wikilink extraction ----------

def test_extract_wikilinks_scalar():
    assert extract_wikilinks_from_value("[[foo]]") == ["foo"]


def test_extract_wikilinks_alias_form():
    assert extract_wikilinks_from_value("[[foo|Display Name]]") == ["foo"]


def test_extract_wikilinks_list():
    assert extract_wikilinks_from_value(["[[a]]", "[[b]]"]) == ["a", "b"]


def test_extract_wikilinks_dedupe_preserves_order():
    assert extract_wikilinks_from_value("[[a]] [[b]] [[a]]") == ["a", "b"]


def test_extract_wikilinks_none_value():
    assert extract_wikilinks_from_value(None) == []


def test_extract_wikilinks_int_value():
    assert extract_wikilinks_from_value(42) == []


def test_extract_edges_skips_system_and_non_edge_fields():
    fm = {
        "type": "Project",                    # NON_EDGE_FIELDS
        "title": "[[would-be-edge-if-not-skipped]]",  # NON_EDGE_FIELDS
        "_indexed_at": "[[also-skipped]]",    # system field
        "related_to": "[[real-edge]]",
        "depends_on": ["[[a]]", "[[b]]"],
    }
    edges = extract_edges(fm)
    rels = sorted([(r, t) for r, t in edges])
    assert ("related_to", "real-edge") in rels
    assert ("depends_on", "a") in rels
    assert ("depends_on", "b") in rels
    assert all(r != "title" for r, _ in rels)
    assert all(r != "type" for r, _ in rels)
    assert all(not r.startswith("_") for r, _ in rels)


def test_extract_aliases_scalar_and_list():
    assert extract_aliases({"aliases": "alpha"}) == ["alpha"]
    assert extract_aliases({"aliases": ["alpha", "beta"]}) == ["alpha", "beta"]
    assert extract_aliases({}) == []
    assert extract_aliases({"aliases": None}) == []
    assert extract_aliases({"aliases": ""}) == []


# ---------- resolver ----------

def test_resolver_filename_priority(tmp_path):
    """Filename match wins over alias match."""
    r = Resolver.empty()
    r.add_doc("a.md", aliases=[], title=None)
    r.add_doc("b.md", aliases=["a"], title=None)
    resolved, ambiguous = r.resolve("a")
    assert resolved == "a.md"
    assert ambiguous is False


def test_resolver_alias_when_no_filename(tmp_path):
    r = Resolver.empty()
    r.add_doc("foo.md", aliases=["alias-x"], title=None)
    resolved, _ = r.resolve("alias-x")
    assert resolved == "foo.md"


def test_resolver_title_when_no_filename_or_alias():
    r = Resolver.empty()
    r.add_doc("foo.md", aliases=[], title="The Big Idea")
    resolved, _ = r.resolve("the big idea")
    assert resolved == "foo.md"


def test_resolver_returns_none_for_ghost():
    r = Resolver.empty()
    r.add_doc("foo.md", aliases=[], title="Foo")
    resolved, _ = r.resolve("does-not-exist")
    assert resolved is None


def test_resolver_strips_md_extension_in_target():
    r = Resolver.empty()
    r.add_doc("foo.md", aliases=[], title=None)
    resolved, _ = r.resolve("foo.md")
    assert resolved == "foo.md"


def test_resolver_case_insensitive():
    r = Resolver.empty()
    r.add_doc("FooBar.md", aliases=[], title=None)
    resolved, _ = r.resolve("foobar")
    assert resolved == "FooBar.md"


def test_resolver_ambiguous_filename_flagged():
    r = Resolver.empty()
    r.add_doc("dir1/foo.md", aliases=[], title=None)
    r.add_doc("dir2/foo.md", aliases=[], title=None)
    resolved, ambiguous = r.resolve("foo")
    assert ambiguous is True
    # Stable: alphabetical pick
    assert resolved == "dir1/foo.md"


def test_resolver_remove_doc_clears_all_indices():
    r = Resolver.empty()
    r.add_doc("foo.md", aliases=["alpha"], title="Foo Title")
    r.remove_doc("foo.md")
    assert r.resolve("foo") == (None, False)
    assert r.resolve("alpha") == (None, False)
    assert r.resolve("foo title") == (None, False)


# ---------- vault round-trip ----------

def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _vault_with_two_docs(vault: Path) -> None:
    _write(vault / "alpha.md", (
        "---\n"
        "type: Project\n"
        "aliases: [first]\n"
        "related_to: [[beta]]\n"
        "---\n"
        "# Alpha\nbody"
    ))
    _write(vault / "beta.md", (
        "---\n"
        "type: Project\n"
        "depends_on: [[alpha]]\n"
        "---\n"
        "# Beta\nbody"
    ))


def test_cold_start_indexes_simple_vault(tmp_path):
    vault = tmp_path / "vault"
    _vault_with_two_docs(vault)
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        stats = ix.cold_start()
        assert stats["inserted"] == 2
        assert store.doc_count() == 2
        # Both edges resolved (no ghosts) since both targets exist as files
        assert store.edge_count() == 2
        assert store.ghost_link_count() == 0
    finally:
        store.close()


def test_cold_start_idempotent(tmp_path):
    vault = tmp_path / "vault"
    _vault_with_two_docs(vault)
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        # Second run: nothing should change
        stats2 = ix.cold_start()
        assert stats2["inserted"] == 0
        assert stats2["updated"] == 0
        assert stats2["skipped"] == 2
        assert stats2["failed"] == 0
    finally:
        store.close()


def test_hash_change_triggers_update(tmp_path):
    vault = tmp_path / "vault"
    _vault_with_two_docs(vault)
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        # Modify alpha
        (vault / "alpha.md").write_text(
            "---\ntype: Project\n---\n# Alpha v2\nnew body"
        )
        stats = ix.cold_start()
        assert stats["updated"] == 1
        assert stats["skipped"] == 1
        # Old edge gone (we removed related_to)
        assert store.edge_count() == 1  # only beta→alpha remains
    finally:
        store.close()


def test_cold_start_creates_ghosts_for_missing_targets(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "a.md", (
        "---\n"
        "type: Project\n"
        "related_to: [[missing-thing]]\n"
        "---\n"
        "# A\n"
    ))
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        assert store.ghost_link_count() == 1
        row = store._conn.execute(
            "SELECT dst_name, ref_count FROM ghost_links"
        ).fetchone()
        assert row["dst_name"] == "missing-thing"
        assert row["ref_count"] == 1
    finally:
        store.close()


def test_cold_start_resolves_forward_references(tmp_path):
    """Vault traversal order shouldn't matter — links must resolve regardless
    of which file is parsed first.

    We force a traversal order by naming files such that 'a' references 'z' and
    'z' references 'a'. With two-phase resolve, both edges resolve."""
    vault = tmp_path / "vault"
    _write(vault / "a.md",
           "---\nrelated_to: [[z]]\n---\n# A\n")
    _write(vault / "z.md",
           "---\nrelated_to: [[a]]\n---\n# Z\n")
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        assert store.ghost_link_count() == 0
        assert store.edge_count() == 2
    finally:
        store.close()


def test_index_file_skips_unchanged_file(tmp_path):
    vault = tmp_path / "vault"
    _vault_with_two_docs(vault)
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        result = ix.index_file(vault / "alpha.md")
        assert result == "skipped"
    finally:
        store.close()


def test_index_file_force_bypasses_hash_check(tmp_path):
    vault = tmp_path / "vault"
    _vault_with_two_docs(vault)
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        result = ix.index_file(vault / "alpha.md", force=True)
        assert result == "updated"
    finally:
        store.close()


def test_delete_file_removes_doc_and_its_edges(tmp_path):
    vault = tmp_path / "vault"
    _vault_with_two_docs(vault)
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        assert store.edge_count() == 2
        ok = ix.delete_file(vault / "alpha.md")
        assert ok is True
        assert store.get_doc("alpha.md") is None
        # alpha's outgoing edges gone; beta's edge to alpha now ghost
        # (because resolver no longer knows about alpha)
        # — but the edge row itself was inserted earlier resolved, and we
        # don't re-resolve on delete-of-target. Verify only alpha's outgoing
        # is gone.
        rows = store._conn.execute(
            "SELECT src_doc_id FROM edges"
        ).fetchall()
        srcs = {r["src_doc_id"] for r in rows}
        assert "alpha.md" not in srcs
    finally:
        store.close()


def test_index_file_inserts_new_file(tmp_path):
    vault = tmp_path / "vault"
    _vault_with_two_docs(vault)
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        # New file added later (simulating a creation event in watch mode)
        _write(vault / "gamma.md",
               "---\nrelated_to: [[alpha]]\n---\n# Gamma\nnew\n")
        result = ix.index_file(vault / "gamma.md")
        assert result == "inserted"
        assert store.doc_count() == 3
    finally:
        store.close()


def test_dot_directories_are_skipped(tmp_path):
    """`.git`, `.obsidian`, etc. must NOT be indexed."""
    vault = tmp_path / "vault"
    _write(vault / "real.md", "---\n---\n# Real\n")
    _write(vault / ".obsidian" / "config.md",
           "---\n---\n# Should not be indexed\n")
    _write(vault / ".git" / "HEAD.md",
           "---\n---\n# Also not\n")
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        ids = store.list_doc_ids()
        assert ids == ["real.md"]
    finally:
        store.close()


def test_non_md_files_ignored(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "a.md", "---\n---\n# A\n")
    _write(vault / "image.png", "fake")
    _write(vault / "notes.txt", "hi")
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        assert store.list_doc_ids() == ["a.md"]
    finally:
        store.close()


def test_doc_id_uses_posix_separators_on_nested(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "projects" / "agentic-1.md", "---\n---\n# Agentic-1\n")
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        assert store.list_doc_ids() == ["projects/agentic-1.md"]
    finally:
        store.close()


def test_indexed_doc_has_aliases_and_title(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "a.md", (
        "---\n"
        "type: Project\n"
        "aliases: [alpha, first]\n"
        "---\n"
        "# Alpha\nbody"
    ))
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        doc = store.get_doc("a.md")
        assert doc is not None
        assert doc.title == "Alpha"
        assert doc.aliases == ["alpha", "first"]
        assert doc.type == "Project"
    finally:
        store.close()


# ---------- regression: Tolaria-style proposals (real-vault shape) ----------

def test_tolaria_proposal_shape_resolves_edges(tmp_path):
    """Mirrors the wikilink-relationships proposal frontmatter that initially
    failed to parse with bare bracket lists."""
    vault = tmp_path / "vault"
    _write(vault / "proposals" / "wikilink-relationships-02.md", (
        "---\n"
        "type: Proposal\n"
        "supersedes: [[wikilink-relationships]]\n"
        "related_to: [[contextgraph]] [[whiteboard]]\n"
        "inspired_by: [[tolaria]]\n"
        "---\n"
        "# Wikilink Relationships v2\n"
    ))
    _write(vault / "proposals" / "wikilink-relationships.md",
           "---\n---\n# Wikilink Relationships\n")
    store = DocStore(tmp_path / "x.db")
    try:
        ix = DocIndexer(store, vault)
        ix.cold_start()
        # 4 edges: 1 supersedes (resolved), 2 related_to (ghost), 1 inspired_by (ghost)
        assert store.edge_count() == 4
        # 3 distinct ghost names: contextgraph, whiteboard, tolaria
        assert store.ghost_link_count() == 3
        # supersedes edge resolved
        rows = store._conn.execute(
            "SELECT dst_doc_id FROM edges WHERE rel_type = 'supersedes'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["dst_doc_id"] == "proposals/wikilink-relationships.md"
    finally:
        store.close()
