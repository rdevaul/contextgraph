"""
test_tag_registry.py — Tests for tag registry lifecycle management.
"""

import pytest
import tempfile
import time
from pathlib import Path

from tag_registry import TagRegistry, TagMetadata


@pytest.fixture
def temp_registry():
    """Create a temporary registry for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        registry = TagRegistry(data_dir=Path(tmpdir))
        yield registry


def test_bootstrap_core_tags(temp_registry):
    """Test that registry bootstraps with core tags from tagger.py."""
    core_tags = temp_registry.get_core_tags()
    assert len(core_tags) > 0
    assert "code" in core_tags
    assert "ai" in core_tags
    assert "networking" in core_tags


def test_get_active_tags(temp_registry):
    """Test that active tags include both core and candidate tags."""
    # Initially only core tags
    active = temp_registry.get_active_tags()
    assert len(active) > 0

    # Add a candidate
    temp_registry.discover([], ["new-candidate"], [])
    active = temp_registry.get_active_tags()
    assert "new-candidate" in active


def test_discover_from_dropped_tags(temp_registry):
    """Test discovery of candidate tags from dropped tags."""
    dropped = ["custom-tag", "another-tag"]
    temp_registry.discover([], dropped, [])

    candidates = temp_registry.get_candidates()
    assert "custom-tag" in candidates
    assert "another-tag" in candidates
    assert candidates["custom-tag"].hits == 1


def test_discover_from_entities(temp_registry):
    """Test discovery of candidate tags from entities."""
    entities = ["OpenClaw", "FastAPI", "Neural Network"]
    temp_registry.discover([], [], entities)

    candidates = temp_registry.get_candidates()
    # Entities should be normalized: lowercased, spaces to dashes
    assert "openclaw" in candidates or "fastapi" in candidates or "neural-network" in candidates


def test_entity_normalization(temp_registry):
    """Test entity normalization to tag names."""
    # Valid entities
    assert temp_registry._normalize_entity_to_tag("FastAPI") == "fastapi"
    assert temp_registry._normalize_entity_to_tag("Neural Network") == "neural-network"
    assert temp_registry._normalize_entity_to_tag("GPT-4") == "gpt-4"

    # Invalid entities (too short, too long, weird chars)
    assert temp_registry._normalize_entity_to_tag("AI") is None  # too short
    assert temp_registry._normalize_entity_to_tag("a" * 50) is None  # too long
    assert temp_registry._normalize_entity_to_tag("foo@bar") is None  # weird chars


def test_update_fired_tags(temp_registry):
    """Test that fired tags get hit counts updated."""
    initial_hits = temp_registry._tags["code"].hits

    temp_registry.discover(["code"], [], [])
    assert temp_registry._tags["code"].hits == initial_hits + 1


def test_salience_calculation(temp_registry):
    """Test salience score calculation."""
    # Create a candidate with known properties
    temp_registry.discover([], ["test-tag"], [])

    # Fire it multiple times to increase frequency
    for _ in range(5):
        temp_registry.discover(["test-tag"], [], [])
        time.sleep(0.01)  # Small delay to ensure different timestamps

    salience = temp_registry.salience("test-tag")
    assert 0.0 <= salience <= 1.0
    assert salience > 0  # Should have some salience due to hits


def test_promotion_threshold(temp_registry):
    """Test that candidates are promoted when meeting criteria."""
    tag_name = "promo-test"

    # Create candidate
    temp_registry.discover([], [tag_name], [])

    # Should not be promoted yet (insufficient hits)
    promoted = temp_registry.promote_candidates()
    assert tag_name not in promoted

    # Fire it enough times
    for _ in range(temp_registry.min_hits_for_promotion):
        temp_registry.discover([tag_name], [], [])

    # Manually set first_seen to past to meet time threshold
    temp_registry._tags[tag_name].first_seen = time.time() - (temp_registry.min_days_for_promotion * 86400 + 1)

    # Now should promote
    promoted = temp_registry.promote_candidates()
    assert tag_name in promoted
    assert temp_registry._tags[tag_name].state == "core"


def test_demotion_stale_tags(temp_registry):
    """Test that stale core tags are demoted to archived."""
    # Pick a core tag and set its last_seen to past
    tag_name = "code"
    temp_registry._tags[tag_name].last_seen = time.time() - (temp_registry.stale_days * 86400 + 1)

    demoted = temp_registry.demote_stale()
    assert tag_name in demoted
    assert temp_registry._tags[tag_name].state == "archived"


def test_force_promote(temp_registry):
    """Test force promotion of a candidate."""
    tag_name = "force-promo"
    temp_registry.discover([], [tag_name], [])

    success = temp_registry.force_promote(tag_name)
    assert success
    assert temp_registry._tags[tag_name].state == "core"

    # Should fail if already core
    success = temp_registry.force_promote(tag_name)
    assert not success


def test_force_demote(temp_registry):
    """Test force demotion of a core tag."""
    tag_name = "code"

    success = temp_registry.force_demote(tag_name)
    assert success
    assert temp_registry._tags[tag_name].state == "archived"

    # Should fail if not core
    success = temp_registry.force_demote(tag_name)
    assert not success


def test_persistence(temp_registry):
    """Test that registry persists to disk and loads correctly."""
    # Add a candidate
    temp_registry.discover([], ["persist-test"], [])

    # Save and reload
    temp_registry.save()
    new_registry = TagRegistry(data_dir=temp_registry.data_dir)

    # Should have the same tags
    assert "persist-test" in new_registry.get_candidates()
    assert new_registry._message_count == temp_registry._message_count


def test_get_all_tags(temp_registry):
    """Test get_all_tags returns structured data for API."""
    temp_registry.discover([], ["candidate-1"], [])

    all_tags = temp_registry.get_all_tags()

    assert "core" in all_tags
    assert "candidate" in all_tags
    assert "archived" in all_tags

    # Core should have tags from bootstrap
    assert len(all_tags["core"]) > 0

    # Candidate should have our test tag
    candidate_names = [t["name"] for t in all_tags["candidate"]]
    assert "candidate-1" in candidate_names

    # Each tag should have required fields
    for tag in all_tags["core"]:
        assert "name" in tag
        assert "hits" in tag
        assert "salience" in tag
        assert "last_seen" in tag


def test_message_count_tracking(temp_registry):
    """Test that message count is tracked correctly."""
    initial_count = temp_registry._message_count

    temp_registry.discover(["code"], [], [])
    assert temp_registry._message_count == initial_count + 1

    temp_registry.discover(["ai"], ["custom"], [])
    assert temp_registry._message_count == initial_count + 2


def test_distinctiveness_calculation(temp_registry):
    """Test that distinctiveness is calculated correctly."""
    # Tag that fires every message has low distinctiveness
    for _ in range(10):
        temp_registry.discover(["code"], [], [])

    # Tag that fires rarely has high distinctiveness
    # First make it a candidate by dropping it
    temp_registry.discover([], ["rare-tag"], [])

    code_tag = temp_registry._tags["code"]
    rare_tag = temp_registry._tags["rare-tag"]

    # Rare tag should have higher distinctiveness
    assert rare_tag.distinctiveness > code_tag.distinctiveness
