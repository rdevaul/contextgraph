"""
test_tager_regression.py — Tagger regression test suite.

Ensures every rule's tags exist in tags.yaml and that key message
patterns always produce the expected tags. This prevents rule changes from
silently breaking tag assignments.

Run: pytest tests/test_tagger_regression.py -v
"""

import pytest
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from tagger import RULES, assign_tags
from features import extract_features
from tag_registry import get_registry


# ── System tag completeness ──────────────────────────────────────────────────

class TestTagRegistryCompleteness:
    """Every tag referenced by a rule must exist in the system registry."""

    def test_all_rule_tags_exist_in_system_tags(self):
        """No rule should reference a tag that doesn't exist in tags.yaml."""
        registry = get_registry()
        active = registry.get_active_tags()
        
        missing = set()
        for rule in RULES:
            for tag in rule.tags:
                if tag not in active:
                    missing.add(f"{rule.name} → {tag}")
        
        assert not missing, (
            f"Rules reference tags that don't exist in tags.yaml:\n"
            + "\n".join(sorted(missing))
            + "\n\nEither add these tags to tags.yaml or remove them from the rules."
        )

    def test_no_duplicate_rule_tags(self):
        """The same exact trigger_words/conditions shouldn't appear twice."""
        seen = set()
        dupes = []
        for rule in RULES:
            key = (rule.name, tuple(sorted(rule.tags)))
            if key in seen:
                dupes.append(rule.name)
            seen.add(key)
        
        assert not dupes, f"Duplicate rules: {dupes}"


# ── Tagger output stability ─────────────────────────────────────────────────

class TestTaggerStability:
    """Key message patterns must always produce specific tags."""

    def _tag(self, user_text, assistant_text=""):
        features = extract_features(user_text, assistant_text)
        return assign_tags(features, user_text, assistant_text)

    # ─── Code / Development ─────────────────────────────────────────

    def test_python_code_detected(self):
        assert "code" in self._tag("```python\ndef hello(): pass\n```")

    def test_javascript_code_detected(self):
        assert "code" in self._tag("```js\nconst x = 1;\n```")

    def test_yaml_config_detected(self):
        assert "infrastructure" in self._tag("```yaml\nname: my-service\n```")

    def test_git_operations(self):
        tags = self._tag("git push origin main")
        assert "code" in tags

    def test_docker_compose(self):
        tags = self._tag("docker-compose up -d")
        assert "devops" in tags or "infrastructure" in tags

    def test_k8s(self):
        tags = self._tag("kubectl get pods -n default")
        assert "devops" in tags or "kubernetes" in tags

    def test_pip_packages(self):
        tags = self._tag("pip install requests")
        assert "code" in tags

    # ─── Networking ───────────────────────────────────────────────────

    def test_tailscale(self):
        tags = self._tag("tailscale is showing offline")
        assert "networking" in tags

    def test_tailscale_status(self):
        tags = self._tag("tailscale status")
        assert "networking" in tags

    def test_dns_localhost(self):
        tags = self._tag("dns resolution for localhost is failing")
        assert "networking" in tags

    def test_http_status_codes(self):
        tags = self._tag("getting a 502 bad gateway")
        assert "networking" in tags

    # ─── Security ─────────────────────────────────────────────────────

    def test_security_token(self):
        tags = self._tag("can you check the security token?")
        assert "security" in tags

    def test_api_key_mention(self):
        tags = self._tag("rotate the api key for production")
        assert "security" in tags

    def test_vulnerability_mention(self):
        tags = self._tag("github found a vulnerability")
        assert "security" in tags

    # ─── Voice / TTS ──────────────────────────────────────────────────

    def test_voice_pwa(self):
        tags = self._tag("voice pwa transcription quality")
        assert "voice-pwa" in tags

    def test_tts_quality(self):
        tags = self._tag("tts audio quality is bad")
        assert "voice-pwa" in tags

    def test_stt_transcription(self):
        tags = self._tag("stt is misrecognizing words")
        assert "voice-pwa" in tags

    # ─── Memory / Context Graph ───────────────────────────────────────

    def test_context_window(self):
        tags = self._tag("the context window keeps filling up")
        assert "context-management" in tags

    def test_compaction(self):
        tags = self._tag("compaction deleted important memories")
        assert "memory-system" in tags

    def test_memory_system(self):
        tags = self._tag("update the memory system config")
        assert "memory-system" in tags

    def test_context_graph(self):
        tags = self._tag("context graph zero return rate")
        assert "contextgraph" in tags or "context-management" in tags

    # ─── Trading / Finance ────────────────────────────────────────────

    def test_options_trading(self):
        tags = self._tag("options position for next week")
        assert "trading" in tags

    def test_portfolio(self):
        tags = self._tag("portfolio rebalancing strategy")
        assert "trading" in tags

    def test_stock_analysis(self):
        tags = self._tag("stock analysis for AAPL")
        assert "trading" in tags

    # ─── AI / LLM ─────────────────────────────────────────────────────

    def test_llm_discussion(self):
        tags = self._tag("comparing claude sonnet vs gpt-4o performance")
        assert "llm" in tags or "ai" in tags

    def test_model_comparison(self):
        tags = self._tag("new model released by anthropic")
        assert "ai" in tags

    def test_agents(self):
        tags = self._tag("the agent keeps hitting rate limits")
        assert "agents" in tags

    # ─── Hardware ─────────────────────────────────────────────────────

    def test_mac_studio(self):
        tags = self._tag("mac studio m4 ultra specs")
        assert "hardware" in tags

    def test_gpu(self):
        tags = self._tag("nvidia gpu availability")
        assert "hardware" in tags

    # ─── Infrastructure / DevOps ──────────────────────────────────────

    def test_database(self):
        tags = self._tag("postgresql is running slow")
        assert "infrastructure" in tags or "devops" in tags

    def test_server(self):
        tags = self._tag("the server is on fire")
        assert "infrastructure" in tags

    # ─── Daily Life ───────────────────────────────────────────────────

    def test_health(self):
        tags = self._tag("how is ivy's health today?")
        assert "health" in tags

    def test_travel(self):
        tags = self._tag("booking flights to SFO next week")
        assert "travel" in tags

    def test_food(self):
        tags = self._tag("what should we have for dinner?")
        assert "food" in tags

    def test_email(self):
        tags = self._tag("check the important emails")
        assert "email" in tags

    def test_weather(self):
        tags = self._tag("weather tomorrow in San Pedro")
        assert "weather" in tags

    # ─── OpenClaw / SybilClaw ─────────────────────────────────────────

    def test_openclaw(self):
        tags = self._tag("openclaw is not responding")
        assert "openclaw" in tags

    def test_sybilclaw(self):
        tags = self._tag("sybilclaw multi-user setup")
        assert "sybilclaw" in tags

    # ─── Ollama ───────────────────────────────────────────────────────

    def test_ollama(self):
        tags = self._tag("ollama pull gemma4 model")
        assert "ollama" in tags

    def test_local_model(self):
        tags = self._tag("local model generation is slow")
        assert "local-compute" in tags or "ollama" in tags


# ── No false positives on generic messages ────────────────────────────────────

class TestNoFalsePositives:
    """Generic messages should not trigger overly specific tags."""

    def _tag(self, user_text, assistant_text=""):
        features = extract_features(user_text, assistant_text)
        return assign_tags(features, user_text, assistant_text)

    def test_hello(self):
        tags = self._tag("hello")
        # Should not have infrastructure, trading, security, etc.
        for tag in tags:
            assert tag not in ("trading", "security", "networking", "devops", "deployment"), \
                "%s should not fire for a greeting" % tag

    def test_thanks(self):
        tags = self._tag("thanks, that's great")
        for tag in tags:
            assert tag not in ("trading", "security", "deployment"), \
                "%s should not fire for a thank-you" % tag

    def test_generic_question(self):
        tags = self._tag("what do you think about that?")
        for tag in tags:
            assert tag not in ("security", "trading", "deployment", "devops"), \
                "%s should not fire for a generic question" % tag

    def test_very_short_inputs_stable(self):
        """Short inputs should produce consistent tags across runs."""
        results = set()
        for _ in range(5):
            tags = self._tag("yes")
            results.add(frozenset(tags))
        assert len(results) == 1, "Tagger is non-deterministic on short input"

    def test_empty_input(self):
        tags = self._tag("")
        assert isinstance(tags, set)
