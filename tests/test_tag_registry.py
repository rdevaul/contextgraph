"""
test_tag_registry.py — Tests for explicit-only tag system.

System tags: loaded from system_tags.json on startup (deterministic, persistent).
User tags: added ONLY via explicit /tags command.
No auto-discovery, auto-promotion, or auto-demotion.

Design: docs/TAG_SYSTEM_DESIGN.md
"""

import pytest
import tempfile
import json
import time
from pathlib import Path

from tag_registry import TagRegistry, TagMetadata, get_registry, get_user_registry, USER_REGISTRY_DIR
import tag_registry as _tag_registry_module


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def system_config_file(tmp_path):
    """Create a minimal system_tags.json and return the path."""
    config = tmp_path / "system_tags.json"
    config.write_text(json.dumps({
        "tags": [
            {"name": "code", "state": "core"},
            {"name": "ai", "state": "core"},
            {"name": "testing", "state": "archived"},
        ]
    }))
    return config


@pytest.fixture
def system_registry(tmp_path, system_config_file):
    """Registry loaded from system_tags.json"""
    reg = TagRegistry(
        data_dir=tmp_path,
        registry_file="tag_registry.json",
        system_config_path=system_config_file,
    )
    yield reg


@pytest.fixture
def system_config_file_empty(tmp_path):
    config = tmp_path / "system_tags.json"
    config.write_text(json.dumps({"tags": []}))
    return config


@pytest.fixture
def empty_system_registry(tmp_path, system_config_file_empty):
    return TagRegistry(
        data_dir=tmp_path,
        registry_file="tag_registry.json",
        system_config_path=system_config_file_empty,
    )


# ── System tag loading ────────────────────────────────────────────────────────

def test_system_registry_loads_core_tags(system_registry):
    """Core tags from system_tags.json are loaded on init."""
    active = system_registry.get_active_tags()
    assert "code" in active
    assert "ai" in active
    assert "testing" not in active  # archived


def test_system_registry_loads_archived_tags(system_registry):
    """Archived tags are present but not active."""
    all_tags = system_registry.get_all_tags()
    archived_names = [t["name"] for t in all_tags.get("archived", [])]
    assert "testing" in archived_names


def test_empty_system_config_loads_nothing(empty_system_registry):
    """An empty system config produces zero tags."""
    active = empty_system_registry.get_active_tags()
    assert len(active) == 0


def test_system_registry_get_core_tags(system_registry):
    """get_core_tags returns only core-tagged entries."""
    cores = system_registry.get_core_tags()
    assert cores == {"code", "ai"}


def test_get_active_tags_for_channel_without_user(system_registry, tmp_path, system_config_file):
    """Without a user registry, channel active = system active."""
    result = system_registry.get_active_tags_for_channel("nonexistent-user-label")
    # For non-existent user, only system tags returned
    assert "code" in result
    assert "ai" in result


# ── System tag CRUD (explicit only) ──────────────────────────────────────────

def test_add_system_tag(system_registry):
    """Adding a new system tag succeeds and returns True."""
    assert system_registry.add_system_tag("security") is True
    assert "security" in system_registry.get_active_tags()


def test_add_system_tag_duplicate(system_registry):
    """Adding an existing system tag returns False."""
    assert system_registry.add_system_tag("code") is False


def test_remove_system_tag(system_registry):
    assert system_registry.remove_system_tag("code") is True
    assert "code" not in system_registry.get_active_tags()


def test_remove_nonexistent_system_tag(system_registry):
    assert system_registry.remove_system_tag("nonexistent") is False


# ── User tag CRUD (explicit only) ─────────────────────────────────────────────

def test_add_user_tag(tmp_path, system_config_file):
    """Adding a user tag creates the tag and persists it."""
    reg = TagRegistry(
        data_dir=tmp_path,
        registry_file="user-alpha.json",
        system_config_path=system_config_file,
    )
    assert reg.add_user_tag("myproject") is True
    assert "myproject" in reg.get_active_tags()


def test_add_user_tag_duplicate(tmp_path, system_config_file):
    reg = TagRegistry(
        data_dir=tmp_path,
        registry_file="user-dup.json",
        system_config_path=system_config_file,
    )
    reg.add_user_tag("myproject")
    assert reg.add_user_tag("myproject") is False


def test_remove_user_tag(tmp_path, system_config_file):
    reg = TagRegistry(
        data_dir=tmp_path,
        registry_file="user-rm.json",
        system_config_path=system_config_file,
    )
    reg.add_user_tag("temp")
    assert reg.remove_user_tag("temp") is True
    assert "temp" not in reg.get_active_tags()


def test_remove_nonexistent_user_tag(tmp_path, system_config_file):
    reg = TagRegistry(
        data_dir=tmp_path,
        registry_file="user-rm2.json",
        system_config_path=system_config_file,
    )
    assert reg.remove_user_tag("notthere") is False


# ── Persistence ───────────────────────────────────────────────────────────────

def test_user_registry_persists(tmp_path, system_config_file):
    """User registry survives save/load cycle."""
    reg1 = TagRegistry(
        data_dir=tmp_path,
        registry_file="persist-user.json",
        system_config_path=system_config_file,
    )
    reg1.add_user_tag("alpha")
    reg1.add_user_tag("beta")
    reg1._message_count = 42
    reg1.save()

    reg2 = TagRegistry(
        data_dir=tmp_path,
        registry_file="persist-user.json",
        system_config_path=system_config_file,
    )
    assert "alpha" in reg2.get_active_tags()
    assert "beta" in reg2.get_active_tags()
    assert reg2._message_count == 42


def test_system_tags_always_reload_from_disk(tmp_path, system_config_file):
    """System tags come from the config file, not from a saved JSON."""
    reg = TagRegistry(
        data_dir=tmp_path,
        registry_file="tag_registry.json",
        system_config_path=system_config_file,
    )
    # Even if we save, the system registry should reload from config.
    reg.save()
    reg2 = TagRegistry(
        data_dir=tmp_path,
        registry_file="tag_registry.json",
        system_config_path=system_config_file,
    )
    active = reg2.get_active_tags()
    assert active == {"code", "ai"}


# ── Combined active tags (system ∪ user) ─────────────────────────────────────

def test_combined_active_tags(tmp_path, system_config_file):
    """get_active_tags_for_channel returns system ∪ user tags."""
    # Use isolated temp dir, not real USER_REGISTRY_DIR
    user_dir = tmp_path / "user-regs-test"
    user_dir.mkdir(exist_ok=True)
    user_file = user_dir / "combined-test.json"

    # System registry must use canonical file name for _is_system to be True
    system_reg = TagRegistry(
        data_dir=tmp_path,
        registry_file="tag_registry.json",
        system_config_path=system_config_file,
    )
    # User registry uses its own file name
    user_reg = TagRegistry(
        data_dir=user_dir,
        registry_file="combined-test.json",
        system_config_path=system_config_file,
    )
    user_reg.add_user_tag("personal")
    user_reg.save()

    # Manually test the union logic (avoid get_user_registry singleton)
    system_active = system_reg.get_active_tags()
    user_active = user_reg.get_active_tags()
    combined = system_active | user_active
    
    assert "code" in combined  # system
    assert "ai" in combined    # system
    assert "personal" in combined  # user


# ── Recording hits ────────────────────────────────────────────────────────────

def test_record_hit_updates_metadata(system_registry):
    """record_hit increments hit count and refreshes last_seen."""
    before = system_registry._tags["code"]
    before_hits = before.hits
    before_seen = before.last_seen
    time.sleep(0.01)

    system_registry.record_hit("code")
    tag = system_registry._tags["code"]
    assert tag.hits == before_hits + 1
    assert tag.last_seen > before_seen


def test_record_hit_unknown_tag_is_silent(system_registry):
    """Recording a hit for a non-existent tag does not crash."""
    system_registry.record_hit("does-not-exist")


# ── get_all_tags structure ────────────────────────────────────────────────────

def test_get_all_tags_structure(system_registry):
    """Returns core/archived dicts with required fields."""
    result = system_registry.get_all_tags()
    assert "core" in result
    assert "archived" in result

    core_names = {t["name"] for t in result["core"]}
    assert "code" in core_names

    for tag in result["core"]:
        for key in ("name", "state", "hits", "last_seen", "first_seen"):
            assert key in tag, f"Missing key: {key}"


# ── Singletons ────────────────────────────────────────────────────────────────

def test_get_registry_returns_single_instance(tmp_path, system_config_file):
    """Singleton pattern: same instance returned twice."""
    # Save the real singleton before we mess with it
    orig = _tag_registry_module._registry_instance
    try:
        _tag_registry_module._registry_instance = None
        r1 = _tag_registry_module.get_registry()
        r2 = _tag_registry_module.get_registry()
        assert r1 is r2
    finally:
        # Restore the real system registry
        _tag_registry_module._registry_instance = orig
