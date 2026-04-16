"""
test_quality_endpoint.py — Tests for quality.py QualityAgent class.

Covers:
- Healthy data: zero-return rate 0, high entropy
- Alert conditions: high zero-return (>0.25), low entropy (<2.0)
- Windowing: old data shouldn't affect recent quality
- Edge cases: empty store, single message, corrupted tags
"""

import pytest
import tempfile
import sqlite3
import time
import json
import math
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from quality import QualityAgent
from store import MessageStore


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_store(tmp_path):
    """Fresh SQLite store for quality testing."""
    db_path = tmp_path / "quality-test.db"
    return MessageStore(str(db_path))


@pytest.fixture
def quality_agent(tmp_store):
    """QualityAgent pointing at a temp store."""
    return QualityAgent(db_path=str(tmp_store._db_path))


# ── Helpers ──────────────────────────────────────────────────────────────────

def insert_msg(store, tags, graph_count=0, offset_sec=0):
    """Convenience: insert a message with given tags and graph_count."""
    conn = sqlite3.connect(store._db_path)
    conn.execute(
        """INSERT INTO messages (
            user_text, assistant_text, timestamp,
            tags, token_count, channel_label, session_id,
            external_id, graph_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test user message",
            "test assistant reply",
            time.time() - offset_sec,
            json.dumps(tags),
            50,
            "rich",
            "quality-session",
            f"q-{time.time()}-{offset_sec}",
            graph_count,
        ),
    )
    conn.commit()
    conn.close()


# ── Healthy data ─────────────────────────────────────────────────────────────

class TestQualityHealthy:
    def test_no_alert_on_empty_store(self, quality_agent):
        r = quality_agent.compute_quality()
        assert r["alert"] is False

    def test_no_alert_on_diverse_data(self, quality_agent):
        """Diverse tags + successful retrievals = healthy."""
        tag_sets = [
            ["ai", "code"], ["networking", "devops"],
            ["voice-pwa", "infra"], ["trading", "finance"],
            ["yapCAD", "plugins"],
        ]
        for i, tags in enumerate(tag_sets * 4):
            insert_msg(quality_agent.store, tags, graph_count=2, offset_sec=i * 300)

        r = quality_agent.compute_quality()
        assert r["alert"] is False
        assert r["tag_entropy"] > 2.0
        assert r["zero_return_rate"] == 0.0

    def test_zero_return_rate_is_accurate(self, quality_agent):
        """75% zero-return should report ~0.75"""
        now = time.time()
        # 15 zero-return turns
        for i in range(15):
            insert_msg(quality_agent.store, ["context"], graph_count=0, offset_sec=now - i * 60)
        # 5 successful turns
        for i in range(5):
            insert_msg(quality_agent.store, ["code"], graph_count=3, offset_sec=now - (20 + i) * 60)

        r = quality_agent.compute_quality()
        assert r["zero_return_turns"] == 15
        assert r["graph_turns"] == 20
        assert 0.7 <= r["zero_return_rate"] <= 0.8


# ── Alert conditions ─────────────────────────────────────────────────────────

class TestQualityAlerts:
    def test_alert_on_high_zero_return(self, quality_agent):
        """Most turns return 0 graph messages → alert."""
        now = time.time()
        for i in range(35):
            insert_msg(quality_agent.store, ["ai"], graph_count=0, offset_sec=i * 60)
        for i in range(5):
            insert_msg(quality_agent.store, ["ai"], graph_count=2, offset_sec=now - (40 + i) * 60)

        r = quality_agent.compute_quality()
        assert r["alert"] is True
        assert r["zero_return_rate"] > 0.25

    def test_alert_on_low_entropy(self, quality_agent):
        """All messages use same tag → low entropy."""
        now = time.time()
        for i in range(30):
            insert_msg(quality_agent.store, ["openclaw"], graph_count=1, offset_sec=i * 300)

        r = quality_agent.compute_quality()
        assert r["alert"] is True
        assert r["tag_entropy"] < 2.0

    def test_no_alert_with_moderate_diversity(self, quality_agent):
        """Even moderate tag diversity should keep entropy above 2.0."""
        tag_pools = [
            ["ai", "code", "testing"],
            ["networking", "devops", "security"],
            ["voice-pwa", "trading"],
            ["yapCAD", "infrastructure"],
            ["agents", "llm"],
        ]
        now = time.time()
        for i, tags in enumerate(tag_pools * 3):
            insert_msg(quality_agent.store, tags, graph_count=2, offset_sec=i * 300)

        r = quality_agent.compute_quality()
        assert r["alert"] is False
        assert r["tag_entropy"] > 2.0


# ── Windowing ────────────────────────────────────────────────────────────────

class TestQualityWindowing:
    def test_old_bad_data_does_not_trigger(self, quality_agent):
        """Bad data older than the analysis window should not alert."""
        now = time.time()
        # Old bad data (3600+ seconds ago, outside the 50-turn window)
        for i in range(40):
            insert_msg(quality_agent.store, ["ai"], graph_count=0, offset_sec=now + 3600 + i * 60)
        # Recent good data
        for i in range(20):
            insert_msg(quality_agent.store, ["code", "ai"], graph_count=3, offset_sec=i * 300)

        r = quality_agent.compute_quality()
        # Only recent turns should count
        assert r["total_turns"] <= 50
        # Recent data is all good (graph_count=3)
        assert r["alert"] is False


# ── Edge cases ───────────────────────────────────────────────────────────────

class TestQualityEdgeCases:
    def test_single_message(self, quality_agent):
        insert_msg(quality_agent.store, ["ai"])
        r = quality_agent.compute_quality()
        assert r["total_turns"] == 1
        assert r["alert"] is False

    def test_empty_tags(self, quality_agent):
        conn = sqlite3.connect(quality_agent.store._db_path)
        conn.execute(
            """INSERT INTO messages (
                user_text, assistant_text, timestamp,
                tags, token_count, channel_label, session_id,
                external_id, graph_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("msg", "reply", time.time(), "[]", 50, "rich", "s", "e", 0),
        )
        conn.commit()
        conn.close()

        r = quality_agent.compute_quality()
        assert r["alert"] is False

    def test_corrupted_tags_gracefully_handled(self, quality_agent):
        """Tags stored as non-JSON should not crash."""
        conn = sqlite3.connect(quality_agent.store._db_path)
        conn.execute(
            """INSERT INTO messages (
                user_text, assistant_text, timestamp,
                tags, token_count, channel_label, session_id,
                external_id, graph_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("msg", "reply", time.time(), "not json at all", 50, "rich", "s", "e", 1),
        )
        conn.commit()
        conn.close()

        r = quality_agent.compute_quality()
        assert r["alert"] is False  # or True, but shouldn't crash

    def test_very_high_message_count(self, quality_agent):
        """Compute quality on 200 messages without issues."""
        tags_list = [
            ["ai", "code"], ["networking"], ["voice-pwa", "infra"],
            ["trading"], ["yapCAD", "plugins", "testing"],
        ]
        now = time.time()
        for i in range(200):
            tags = tags_list[i % len(tags_list)]
            insert_msg(quality_agent.store, tags, graph_count=i % 3, offset_sec=i * 60)

        r = quality_agent.compute_quality()
        assert r["total_turns"] == 200
        assert isinstance(r["tag_entropy"], float)
