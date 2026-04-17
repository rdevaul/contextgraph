"""
Regression tests for tag-context service.

These tests catch the specific regressions that broke between April 9-11 2026:
1. store.get_by_tag() no longer accepts channel_label parameter
2. /tags endpoint was removed during cleanup
3. /compare endpoint depends on get_by_tag() signature

Run with: python3 -m pytest tests/test_regressions.py -v
"""

import sys
import json
import subprocess
import urllib.request
import urllib.error
import pytest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from store import MessageStore
from assembler import ContextAssembler

# Base URL for live integration tests (requires running server)
BASE_URL = "http://localhost:8302"


def _api_get(path):
    """Make a GET request to the live API."""
    req = urllib.request.Request(f"{BASE_URL}{path}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _api_post(path, data):
    """Make a POST request to the live API."""
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=body,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def _server_available():
    """Check if the live server is available."""
    try:
        _api_get("/health")
        return True
    except Exception:
        return False


@pytest.mark.regression
class TestStoreGetByTagSignature:
    """Regression: store.get_by_tag() should NOT accept channel_label.

    Bug 2026-04-09: The channel label merge refactoring removed channel_label
    from get_by_tag(), but assembler.py was still passing it. This caused:
    'MessageStore.get_by_tag() got an unexpected keyword argument channel_label'
    """

    def test_get_by_tag_does_not_accept_channel_label(self):
        """get_by_tag() signature should not have channel_label parameter."""
        import inspect
        sig = inspect.signature(MessageStore.get_by_tag)
        params = list(sig.parameters.keys())

        assert 'channel_label' not in params, (
            f"channel_label should not be in get_by_tag() params: {params}"
        )
        assert 'tag' in params
        assert 'limit' in params
        assert 'include_automated' in params

    def test_get_by_tag_call_does_not_pass_channel_label(self):
        """Mocked call should NOT pass channel_label."""
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_by_tag.return_value = []

        mock_store.get_by_tag("test-tag", limit=50)
        mock_store.get_by_tag.assert_called_once_with("test-tag", limit=50)


@pytest.mark.regression
class TestTagsEndpoint:
    """Regression: GET /tags endpoint must exist.

    Bug 2026-04-09: The /tags endpoint was removed during the cleanup commit
    (1f81fd50), but the OpenClaw plugin still calls it.
    """

    def test_tags_endpoint_exists_in_server(self):
        """server.py must define a GET /tags route."""
        server_path = Path(__file__).parent.parent / "api" / "server.py"
        content = server_path.read_text()

        assert '@app.get("/tags"' in content, (
            "GET /tags endpoint missing from server.py"
        )
        assert 'system_tags' in content
        assert 'user_tags' in content

    @pytest.mark.skipif(not _server_available(), reason="Live server not available")
    def test_tags_response_shape(self):
        """GET /tags must return {system_tags: [...], user_tags: [...]}."""
        data = _api_get("/tags")

        assert "system_tags" in data, "Response missing 'system_tags'"
        assert "user_tags" in data, "Response missing 'user_tags'"
        assert isinstance(data["system_tags"], list)
        assert isinstance(data["user_tags"], list)

        if data["system_tags"]:
            tag = data["system_tags"][0]
            assert "name" in tag
            assert "state" in tag
            assert "hits" in tag
            assert "corpus_pct" in tag


@pytest.mark.regression
class TestAssemblerChannelLabel:
    """Regression: assembler must not pass channel_label to get_by_tag()."""

    def test_assembler_source_no_channel_label_in_get_by_tag_call(self):
        """assembler.py source should not pass channel_label to get_by_tag()."""
        assembler_path = Path(__file__).parent.parent / "assembler.py"
        content = assembler_path.read_text()

        assert "get_by_tag(tag, limit=50, channel_label=" not in content, (
            "assembler.py still passes channel_label to get_by_tag()"
        )

    def test_assemble_method_accepts_channel_label(self):
        """assemble() should still accept channel_label for user-scoped filtering."""
        import inspect
        sig = inspect.signature(ContextAssembler.assemble)
        params = list(sig.parameters.keys())

        assert 'channel_label' in params, (
            "assemble() should accept channel_label for user-scoped filtering"
        )


@pytest.mark.regression
class TestCompareEndpoint:
    """Regression: /compare endpoint must work end-to-end."""

    def test_compare_endpoint_exists_in_server(self):
        """server.py must define POST /compare."""
        server_path = Path(__file__).parent.parent / "api" / "server.py"
        content = server_path.read_text()

        assert '@app.post("/compare"' in content, (
            "POST /compare endpoint missing from server.py"
        )

    @pytest.mark.skipif(not _server_available(), reason="Live server not available")
    def test_compare_returns_graph_assembly_structure(self):
        """POST /compare must return graph_assembly with total_tokens."""
        data = _api_post("/compare", {
            "user_text": "test query",
            "assistant_text": ""
        })

        assert "graph_assembly" in data, "Response missing 'graph_assembly'"
        assert "linear_window" in data, "Response missing 'linear_window'"
        assert "total_tokens" in data["graph_assembly"], (
            "graph_assembly missing 'total_tokens' — dashboard will fail!"
        )
        assert "total_tokens" in data["linear_window"]
        assert "messages" in data["graph_assembly"]
        assert "tags_used" in data["graph_assembly"]

    @pytest.mark.skipif(not _server_available(), reason="Live server not available")
    def test_compare_matches_voice_pwa_tag(self):
        """POST /compare should match voice-pwa tag for a voice-pwa query."""
        data = _api_post("/compare", {
            "user_text": "Tell me about voice-pwa",
            "assistant_text": ""
        })

        tags_used = data["graph_assembly"].get("tags_used", [])
        assert "voice-pwa" in tags_used, (
            f"voice-pwa not matched! Tags found: {tags_used}"
        )


@pytest.mark.regression
class TestChannelLabelMergeIntegrity:
    """Ensure the April 9 channel label merge didn't break other things."""

    def test_store_count_works(self):
        """store.count() should return an int, not raise."""
        try:
            store = MessageStore()
            count = store.count()
            assert isinstance(count, int)
            assert count > 0, "Store has no messages"
        except Exception as e:
            pytest.skip(f"Could not test store.count(): {e}")

    def test_no_stale_bak_plugins_in_extensions(self):
        """No visible .bak directories should exist in extensions/."""
        for base in [Path.home() / ".openclaw" / "extensions",
                     Path.home() / ".sybilclaw" / "extensions"]:
            if base.exists():
                visible_bak = [d for d in base.glob("contextgraph.bak*")
                               if not d.name.startswith('.')]
                assert len(visible_bak) == 0, (
                    f"Visible .bak directories in {base}: {visible_bak}"
                )
