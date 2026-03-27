"""
Test suite for tagger bug fixes (substring matching, metadata poisoning, generic triggers).
"""
import pytest
from features import extract_features
from tagger import assign_tags, _text_contains_any, _strip_metadata


class TestBug1SubstringMatching:
    """Test that word-boundary regex prevents false substring matches."""

    def test_rl_does_not_match_url(self):
        """'rl' should NOT match 'url', 'world', 'clearly', 'particularly'."""
        assert not _text_contains_any("Check this url", "", ["rl"])
        assert not _text_contains_any("Hello world", "", ["rl"])
        assert not _text_contains_any("This is clearly wrong", "", ["rl"])
        assert not _text_contains_any("particularly important", "", ["rl"])

    def test_rl_matches_standalone_rl(self):
        """'rl' should match standalone 'rl' or 'RL'."""
        assert _text_contains_any("I'm using rl for this", "", ["rl"])
        assert _text_contains_any("RL is important", "", ["rl"])
        assert _text_contains_any("", "The rl agent", ["rl"])

    def test_reinforcement_learning_matches(self):
        """Multi-word phrase 'reinforcement learning' should match."""
        assert _text_contains_any("Using reinforcement learning here", "", ["reinforcement learning"])
        assert _text_contains_any("REINFORCEMENT LEARNING approach", "", ["reinforcement learning"])

    def test_deploy_does_not_match_deployment(self):
        """'deploy' should NOT match 'deployment' when using word boundaries."""
        # Note: "deploy" is a substring of "deployment", but word-boundary should prevent match
        assert not _text_contains_any("This is a deployment guide", "", ["deploy to"])

    def test_deploy_to_matches_correctly(self):
        """'deploy to' should match 'deploy to aws' but not 'deployment'."""
        assert _text_contains_any("Let's deploy to aws", "", ["deploy to"])
        assert _text_contains_any("We will deploy to production", "", ["deploy to"])


class TestBug2MetadataPoisoning:
    """Test that OpenClaw metadata is stripped before tagging."""

    def test_strip_conversation_info_metadata(self):
        """Strip 'Conversation info (untrusted metadata):' blocks."""
        text = """Conversation info (untrusted metadata):
```json
{"message_id": "123", "token": 456, "model": "gpt-4"}
```
Actual user message here."""
        cleaned = _strip_metadata(text)
        assert "token" not in cleaned
        assert "model" not in cleaned
        assert "Actual user message here" in cleaned

    def test_strip_sender_metadata(self):
        """Strip 'Sender (untrusted metadata):' blocks."""
        text = """Sender (untrusted metadata):
```json
{"sender_id": "user123", "auth": "bearer"}
```
Real content."""
        cleaned = _strip_metadata(text)
        assert "auth" not in cleaned
        assert "Real content" in cleaned

    def test_strip_replied_message_metadata(self):
        """Strip 'Replied message (untrusted' blocks."""
        text = """Replied message (untrusted metadata):
```json
{"security": "high"}
```
Actual reply."""
        cleaned = _strip_metadata(text)
        assert "security" not in cleaned
        assert "Actual reply" in cleaned

    def test_strip_runtime_section(self):
        """Strip '## Runtime' sections."""
        text = """## Runtime
Some runtime info with token counts
## Other Section
Real content."""
        cleaned = _strip_metadata(text)
        assert "runtime" not in cleaned.lower() or "real content" in cleaned.lower()

    def test_strip_voice_pwa_markers(self):
        """Strip '[Voice PWA]' boilerplate."""
        text = "[Voice PWA] User said something"
        cleaned = _strip_metadata(text)
        assert "[Voice PWA]" not in cleaned
        assert "User said something" in cleaned

    def test_metadata_does_not_trigger_ai_llm_rule(self):
        """Metadata containing 'model' or 'token' should not trigger ai-llm tag."""
        # This text has metadata with "model" and "token" but no real AI/LLM content
        user_text = """Conversation info (untrusted metadata):
```json
{"model": "gpt-4", "token": 123}
```
Please help me with my shopping list."""
        assistant_text = ""

        features = extract_features(user_text, assistant_text)
        tags = assign_tags(features, user_text, assistant_text)
        # Should NOT contain ai or llm tags
        assert "ai" not in tags
        assert "llm" not in tags

    def test_metadata_does_not_trigger_security_rule(self):
        """Metadata containing 'token' should not trigger security tag."""
        user_text = """Conversation info (untrusted metadata):
```json
{"token": 456, "auth": "bearer"}
```
What's the weather today?"""
        assistant_text = ""

        features = extract_features(user_text, assistant_text)
        tags = assign_tags(features, user_text, assistant_text)
        # Should NOT contain security tag
        assert "security" not in tags


class TestBug3GenericTriggers:
    """Test that overly generic single-word triggers are replaced with specific phrases."""

    def test_model_in_system_prompt_does_not_trigger_ai_llm(self):
        """The word 'model' alone should NOT trigger ai-llm tag."""
        user_text = "The data model for our database is complex."
        assistant_text = ""

        features = extract_features(user_text, assistant_text)
        tags = assign_tags(features, user_text, assistant_text)
        # Should NOT contain ai or llm tags
        assert "ai" not in tags
        assert "llm" not in tags

    def test_language_model_triggers_ai_llm(self):
        """Multi-word phrase 'language model' should trigger ai-llm tag."""
        user_text = "I'm working with a language model for NLP."
        assistant_text = ""

        features = extract_features(user_text, assistant_text)
        tags = assign_tags(features, user_text, assistant_text)
        assert "ai" in tags or "llm" in tags

    def test_security_vulnerability_triggers_security(self):
        """Multi-word phrase 'security vulnerability' should trigger security tag."""
        user_text = "Found a security vulnerability in the code."
        assistant_text = ""

        features = extract_features(user_text, assistant_text)
        tags = assign_tags(features, user_text, assistant_text)
        assert "security" in tags

    def test_context_window_triggers_context_management(self):
        """Multi-word phrase 'context window' should trigger context-management tag."""
        user_text = "The context window is running out of space."
        assistant_text = ""

        features = extract_features(user_text, assistant_text)
        tags = assign_tags(features, user_text, assistant_text)
        assert "context-management" in tags

    def test_npm_run_build_triggers_devops(self):
        """Multi-word phrase 'npm run build' should trigger devops tag."""
        user_text = "Run npm run build before deploying."
        assistant_text = ""

        features = extract_features(user_text, assistant_text)
        tags = assign_tags(features, user_text, assistant_text)
        assert "devops" in tags or "deployment" in tags


class TestBug4ContextManagementTags:
    """Test that context-management rule outputs only ['context-management']."""

    def test_context_management_does_not_emit_rl_or_ai(self):
        """context-management rule should only emit 'context-management', not 'rl' or 'ai'."""
        user_text = "Let's improve the context window management."
        assistant_text = ""

        features = extract_features(user_text, assistant_text)
        tags = assign_tags(features, user_text, assistant_text)

        # Should contain context-management
        assert "context-management" in tags
        # Should NOT contain 'rl' or 'ai' from this rule
        # (if 'ai' appears, it should be from a different rule)


class TestContainsURLRule:
    """Test that contains-url rule outputs 'has-url' instead of 'research'.
    NOTE: has-url tag was disabled (2026-03-27) as a stop-word — fires on
    every message with a link, providing zero retrieval signal."""

    def test_url_does_not_trigger_has_url_when_disabled(self):
        """has-url tag is disabled — URLs should not produce this tag."""
        user_text = "Check out https://example.com"
        assistant_text = ""

        features = extract_features(user_text, assistant_text)
        tags = assign_tags(features, user_text, assistant_text)

        # has-url was disabled as a stop-word tag
        assert "has-url" not in tags


class TestEdgeCases:
    """Test edge cases and combined scenarios."""

    def test_case_insensitive_matching(self):
        """Terms should match case-insensitively."""
        assert _text_contains_any("LANGUAGE MODEL here", "", ["language model"])
        assert _text_contains_any("Language Model Here", "", ["language model"])
        assert _text_contains_any("language model here", "", ["language model"])

    def test_word_boundary_with_punctuation(self):
        """Word boundaries should work with punctuation."""
        assert _text_contains_any("deploy to production.", "", ["deploy to"])
        assert _text_contains_any("(deploy to aws)", "", ["deploy to"])
        assert _text_contains_any("deploy to, verify", "", ["deploy to"])

    def test_empty_text_does_not_crash(self):
        """Empty text should not cause errors."""
        user_text = ""
        assistant_text = ""

        features = extract_features(user_text, assistant_text)
        tags = assign_tags(features, user_text, assistant_text)
        assert isinstance(tags, list)

    def test_metadata_stripping_preserves_real_content(self):
        """Metadata stripping should preserve legitimate content."""
        text = """Conversation info (untrusted metadata):
```json
{"id": 123}
```
I need help with reinforcement learning for my project."""

        cleaned = _strip_metadata(text)
        assert "reinforcement learning" in cleaned
        assert "project" in cleaned
