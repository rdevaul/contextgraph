"""Tests for channel access rules and per-agent memory filtering."""

import sys
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from channel_access import AGENT_CHANNEL_ACCESS, get_allowed_labels, filter_turns_for_agent


class TestGetAllowedLabels:
    """Test get_allowed_labels returns correct channels for each agent."""

    def test_main_agent(self):
        assert get_allowed_labels("main") == ["rich-dm", "rich-household"]

    def test_glados_rich(self):
        assert get_allowed_labels("glados-rich") == ["rich-dm", "rich-household"]

    def test_glados_household(self):
        assert get_allowed_labels("glados-household") == ["rich-household"]

    def test_glados_dana(self):
        assert get_allowed_labels("glados-dana") == ["dana-dm", "rich-household"]

    def test_glados_terry(self):
        assert get_allowed_labels("glados-terry") == ["terry-dm", "rich-household"]

    def test_glados_lily(self):
        assert get_allowed_labels("glados-lily") == ["lily-dm"]

    def test_glados_lynae(self):
        assert get_allowed_labels("glados-lynae") == ["lynae-dm"]

    def test_unknown_agent_returns_empty(self):
        assert get_allowed_labels("unknown-agent") == []

    def test_all_agents_have_at_least_one_channel(self):
        for agent_id, channels in AGENT_CHANNEL_ACCESS.items():
            assert len(channels) > 0, f"Agent {agent_id} has no channels"


class TestFilterTurnsForAgent:
    """Test filter_turns_for_agent correctly filters by channel label."""

    def _make_turn(self, channel_label=None, text="hello"):
        turn = {"user_text": text, "assistant_text": "response"}
        if channel_label is not None:
            turn["channel_label"] = channel_label
        return turn

    def test_rich_sees_rich_dm(self):
        turns = [self._make_turn("rich-dm")]
        result = filter_turns_for_agent(turns, "glados-rich")
        assert len(result) == 1

    def test_rich_sees_rich_household(self):
        turns = [self._make_turn("rich-household")]
        result = filter_turns_for_agent(turns, "glados-rich")
        assert len(result) == 1

    def test_rich_does_not_see_dana_dm(self):
        turns = [self._make_turn("dana-dm")]
        result = filter_turns_for_agent(turns, "glados-rich")
        assert len(result) == 0

    def test_lily_dm_not_visible_to_rich(self):
        """Verify lily-dm turns don't appear in rich's filtered set."""
        turns = [
            self._make_turn("lily-dm", "lily private message"),
            self._make_turn("rich-dm", "rich private message"),
        ]
        result = filter_turns_for_agent(turns, "glados-rich")
        assert len(result) == 1
        assert result[0]["user_text"] == "rich private message"

    def test_lily_dm_not_visible_to_main(self):
        """Verify lily-dm turns don't appear in main agent's filtered set."""
        turns = [self._make_turn("lily-dm")]
        result = filter_turns_for_agent(turns, "main")
        assert len(result) == 0

    def test_unlabeled_turns_excluded(self):
        """Verify unlabeled turns are excluded from all agents."""
        turns = [self._make_turn(None, "no label")]
        for agent_id in AGENT_CHANNEL_ACCESS:
            result = filter_turns_for_agent(turns, agent_id)
            assert len(result) == 0, f"Unlabeled turn leaked to {agent_id}"

    def test_dana_sees_household_and_own_dm(self):
        turns = [
            self._make_turn("dana-dm"),
            self._make_turn("rich-household"),
            self._make_turn("rich-dm"),
            self._make_turn("terry-dm"),
        ]
        result = filter_turns_for_agent(turns, "glados-dana")
        labels = [t["channel_label"] for t in result]
        assert set(labels) == {"dana-dm", "rich-household"}

    def test_household_only_sees_household(self):
        turns = [
            self._make_turn("rich-dm"),
            self._make_turn("rich-household"),
            self._make_turn("dana-dm"),
        ]
        result = filter_turns_for_agent(turns, "glados-household")
        assert len(result) == 1
        assert result[0]["channel_label"] == "rich-household"

    def test_unknown_agent_sees_nothing(self):
        turns = [
            self._make_turn("rich-dm"),
            self._make_turn("rich-household"),
        ]
        result = filter_turns_for_agent(turns, "unknown-agent")
        assert len(result) == 0

    def test_mixed_labeled_and_unlabeled(self):
        turns = [
            self._make_turn("rich-dm", "labeled"),
            self._make_turn(None, "unlabeled"),
            self._make_turn("rich-household", "also labeled"),
        ]
        result = filter_turns_for_agent(turns, "main")
        assert len(result) == 2
        texts = [t["user_text"] for t in result]
        assert "unlabeled" not in texts
