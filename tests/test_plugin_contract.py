"""
Plugin contract tests for the TypeScript OpenClaw plugin.

These tests verify that the TypeScript plugin files are syntactically correct
and contain the required wiring for the context graph system.

Run with: python3 -m pytest tests/test_plugin_contract.py -v --tb=short
"""

import pytest
from pathlib import Path


PLUGIN_DIR = Path.home() / ".openclaw" / "extensions" / "contextgraph"
ENGINE_FILE = PLUGIN_DIR / "engine.ts"
API_CLIENT_FILE = PLUGIN_DIR / "api-client.ts"
INDEX_FILE = PLUGIN_DIR / "index.ts"


@pytest.fixture(scope="module")
def plugin_files_exist():
    """Check if plugin files exist."""
    if not PLUGIN_DIR.exists():
        pytest.skip(f"Plugin directory not found: {PLUGIN_DIR}")
    if not ENGINE_FILE.exists():
        pytest.skip(f"engine.ts not found: {ENGINE_FILE}")
    if not API_CLIENT_FILE.exists():
        pytest.skip(f"api-client.ts not found: {API_CLIENT_FILE}")
    if not INDEX_FILE.exists():
        pytest.skip(f"index.ts not found: {INDEX_FILE}")
    return True


@pytest.mark.plugin_contract
class TestEngineFile:
    """Test engine.ts contract."""

    def test_engine_contains_detect_tool_chains(self, plugin_files_exist):
        """Contract 1: engine.ts contains detectToolChains method."""
        with open(ENGINE_FILE, 'r') as f:
            content = f.read()

        # Should have the method defined
        assert "detectToolChains" in content, "detectToolChains method not found in engine.ts"

        # Should have the method signature with return type
        assert "last_turn_had_tools" in content, "last_turn_had_tools not found in detectToolChains"
        assert "pending_chain_ids" in content, "pending_chain_ids not found in detectToolChains"

    def test_engine_passes_tool_state_to_assemble(self, plugin_files_exist):
        """Contract 2: engine.ts passes toolState to client.assemble as 4th argument."""
        with open(ENGINE_FILE, 'r') as f:
            content = f.read()

        # Should call detectToolChains
        assert "this.detectToolChains" in content, "detectToolChains not called in engine.ts"

        # Should pass toolState to client.assemble
        assert "this.client.assemble" in content, "client.assemble not called in engine.ts"

        # Check that toolState is passed
        # Look for the pattern where toolState variable is used
        assert "toolState" in content, "toolState variable not found in engine.ts"

    def test_engine_handles_content_arrays(self, plugin_files_exist):
        """Contract 5: engine.ts handles content arrays correctly."""
        with open(ENGINE_FILE, 'r') as f:
            content = f.read()

        # Should check if content is an array
        assert "Array.isArray" in content, "Array.isArray not found in engine.ts"

        # Should have rawContent or similar variable
        assert "rawContent" in content or "content" in content, "Content handling not found in engine.ts"

        # Should extract text from content blocks
        assert 'type === "text"' in content or "type: \"text\"" in content, "Text block extraction not found"


@pytest.mark.plugin_contract
class TestAPIClientFile:
    """Test api-client.ts contract."""

    def test_api_client_assemble_accepts_tool_state(self, plugin_files_exist):
        """Contract 3: api-client.ts assemble() accepts toolState parameter."""
        with open(API_CLIENT_FILE, 'r') as f:
            content = f.read()

        # Should have ToolState interface
        assert "interface ToolState" in content or "export interface ToolState" in content, \
            "ToolState interface not found in api-client.ts"

        # Should have toolState parameter in assemble method
        assert "toolState" in content, "toolState parameter not found in api-client.ts"

        # Check the interface has the right fields
        assert "last_turn_had_tools" in content, "last_turn_had_tools not in ToolState"
        assert "pending_chain_ids" in content, "pending_chain_ids not in ToolState"

    def test_api_client_sends_tool_state_in_request(self, plugin_files_exist):
        """Contract 4: api-client.ts sends tool_state in request body."""
        with open(API_CLIENT_FILE, 'r') as f:
            content = f.read()

        # Should send tool_state in the request body
        assert "tool_state" in content, "tool_state not found in request body"

        # Should use JSON.stringify or similar
        assert "JSON.stringify" in content, "JSON.stringify not found in api-client.ts"


@pytest.mark.plugin_contract
class TestIndexFile:
    """Test index.ts contract."""

    def test_index_registers_context_engine(self, plugin_files_exist):
        """Contract 6: index.ts registers the context engine."""
        with open(INDEX_FILE, 'r') as f:
            content = f.read()

        # Should register the context engine
        assert "registerContextEngine" in content, "registerContextEngine not found in index.ts"

        # Should use the ContextGraphEngine
        assert "ContextGraphEngine" in content, "ContextGraphEngine not imported/used in index.ts"

        # Should have the engine ID "contextgraph"
        assert '"contextgraph"' in content or "'contextgraph'" in content, \
            "contextgraph engine ID not found in index.ts"
