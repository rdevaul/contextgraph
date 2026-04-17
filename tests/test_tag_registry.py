"""
test_tag_registry.py — Tests for explicit-only tag system driven by YAML.

YAML is single source of truth for tag definitions.
JSON (tag_registry.json) is only runtime stats (hits, timestamps).
"""

import json
import tempfile
import time
from pathlib import Path

import pytest

from tag_registry import (
    TagRegistry,
    TagConfig,
    _load_yaml,
    clear_user_registry_cache,
    get_user_registry,
    reload_registry,
)
import tag_registry as _mod


@pytest.fixture
def yaml_path(tmp_path):
    """Create a test tags.yaml and return path."""
    p = tmp_path / "tags.yaml"
    p.write_text("""
tags:
  - name: code
    description: "Code and development"
    keywords: ["code", "def ", "class "]
    patterns: ["```"]
  - name: ai
    description: "AI and machine learning"
    keywords: ["AI", "machine learning", "model"]
  - name: testing
    description: "Testing and QA"
    keywords: ["test", "pytest"]
    enabled: false
""")
    return p


@pytest.fixture
def system_reg(tmp_path, yaml_path):
    """System-mode registry backed by the test yaml_path.
    Saves and restores the global singleton to avoid cross-test pollution."""
    old = _mod._registry_instance
    _mod._registry_instance = None  # Clear so this test's registry is used as singleton
    reg = TagRegistry(
        data_dir=tmp_path,
        registry_file="tag_registry.json",
        yaml_path=yaml_path,
        is_system=True,
    )
    _mod._registry_instance = reg
    yield reg
    _mod._registry_instance = old  # Restore


@pytest.fixture
def empty_yaml(tmp_path):
    p = tmp_path / "tags.yaml"
    p.write_text("tags: []\n")
    return p


@pytest.fixture
def empty_system_reg(tmp_path, empty_yaml):
    return TagRegistry(
        data_dir=tmp_path,
        registry_file="tag_registry.json",
        yaml_path=empty_yaml,
        is_system=True,
    )


# ── YAML loading ─────────────────────────────────────────────────────────────

def test_load_yaml_loads_enabled_tags(yaml_path):
    defs = _load_yaml(yaml_path)
    assert "code" in defs
    assert "ai" in defs


def test_load_yaml_marks_disabled_as_archived(yaml_path):
    defs = _load_yaml(yaml_path)
    assert defs["testing"].state == "archived"
    assert defs["testing"].enabled is False


def test_load_yaml_missing_file():
    assert _load_yaml(Path("/nonexistent/tags.yaml")) == {}


# ── System registry loads core from YAML ──────────────────────────────────────

def test_system_registry_loads_cores_tags(system_reg):
    active = system_reg.get_active_tags()
    assert "code" in active
    assert "ai" in active
    assert "testing" not in active


def test_system_registry_get_core_tags(system_reg):
    cores = system_reg.get_core_tags()
    assert cores == system_reg.get_active_tags()


def test_empty_system_has_no_active_tags(empty_system_reg):
    assert len(empty_system_reg.get_active_tags()) == 0


# ── System tag CRUD ──────────────────────────────────────────────────────────

def test_add_system_tag(system_reg):
    assert system_reg.add_system_tag("security") is True
    assert "security" in system_reg.get_active_tags()


def test_add_system_tag_duplicate(system_reg):
    assert system_reg.add_system_tag("code") is False


def test_remove_system_tag(system_reg):
    assert system_reg.remove_system_tag("code") is True
    assert "code" not in system_reg.get_active_tags()


def test_remove_nonexistent_system_tag(system_reg):
    assert system_reg.remove_system_tag("nonexistent") is False


# ── YAML is always authoritative (never overridden by JSON) ──────────────────

def test_system_tags_always_reload_from_yaml(system_reg, yaml_path):
    """System tags come from YAML on every load, regardless of JSON overlay."""
    system_reg.save()
    reg2 = TagRegistry(
        data_dir=system_reg.data_dir,
        registry_file="tag_registry.json",
        yaml_path=yaml_path,
        is_system=True,
    )
    assert "code" in reg2.get_active_tags()
    assert "ai" in reg2.get_active_tags()
    assert "testing" not in reg2.get_active_tags()


# ── User tags ────────────────────────────────────────────────────────────────

def test_add_user_tag_persists(system_reg):
    system_reg.add_user_tag("myproject")
    assert "myproject" in system_reg.get_active_tags()
    # Verify JSON persisted
    state_path = system_reg.data_dir / system_reg.registry_file
    data = json.loads(state_path.read_text())
    assert any(t["name"] == "myproject" for t in data["tags"])


def test_add_user_tag_duplicate(system_reg):
    system_reg.add_user_tag("myproject")
    assert system_reg.add_user_tag("myproject") is False


def test_remove_user_tag(system_reg):
    system_reg.add_user_tag("temp")
    assert system_reg.remove_user_tag("temp") is True
    assert "temp" not in system_reg.get_active_tags()


def test_remove_nonexistent_user_tag(system_reg):
    assert system_reg.remove_user_tag("notthere") is False


# ── Combined active tags (system ∪ user) ────────────────────────────────────

def test_combined_active_tags(system_reg):
    """get_active_tags_for_channel returns system + user tags for this registry."""
    system_reg.add_user_tag("personal")
    # get_active_tags_for_channel calls the global singleton, which is system_reg in this fixture
    active = system_reg.get_active_tags()
    assert "code" in active
    assert "ai" in active
    assert "personal" in active


# ── Record hit ───────────────────────────────────────────────────────────────

def test_record_hit(system_reg):
    system_reg.record_hit("code")
    all_tags = system_reg.get_all_tags()
    code_tags = [t for t in all_tags["core"] if t["name"] == "code"]
    assert code_tags[0]["hits"] >= 1


def test_record_hit_unknown_silent(system_reg):
    system_reg.record_hit("does-not-exist")


# ── get_all_tags structure ───────────────────────────────────────────────────

def test_get_all_tags_structure(system_reg):
    result = system_reg.get_all_tags()
    assert "core" in result
    assert "archived" in result

    core_names = {t["name"] for t in result["core"]}
    assert "code" in core_names

    for tag in result["core"]:
        for key in ("name", "state", "hits", "last_seen", "first_seen"):
            assert key in tag


# ── get_active_tags_for_channel ──────────────────────────────────────────────

def test_channel_with_no_user_registry(system_reg):
    result = system_reg.get_active_tags_for_channel("nonexistent-user")
    assert "code" in result
    assert "ai" in result


# ── Singleton ────────────────────────────────────────────────────────────────

def test_get_registry_returns_instance():
    orig = _mod._registry_instance
    try:
        _mod._registry_instance = None
        r1 = _mod.get_registry()
        r2 = _mod.get_registry()
        assert r1 is r2
    finally:
        _mod._registry_instance = orig


def test_reload_registry():
    orig = _mod._registry_instance
    try:
        _mod._registry_instance = None
        _mod.get_registry()
        old = _mod._registry_instance
        _mod.reload_registry()
        assert _mod._registry_instance is not old
    finally:
        _mod._registry_instance = orig
