"""
tagger.py — v0 structured-program tagger for the tag-context system.

This is the hand-written baseline tagger. Each rule is an explicit
structured program over MessageFeatures. This is the "genome" prototype —
future GP-evolved taggers will follow the same interface.
"""

from dataclasses import dataclass
from typing import Callable, List, Optional, Set

from features import MessageFeatures


# ── Tag vocabulary (seeded-open core) ────────────────────────────────────────

CORE_TAGS = {
    # Infrastructure / system
    "networking", "security", "infrastructure", "devops",
    # Software / code
    "code", "api", "debugging", "deployment",
    # AI / ML
    "ai", "llm", "context-management", "rl",
    # Project-specific (extend as needed)
    "voice-pwa", "shopping-list", "openclaw", "yapCAD",
    # General
    "planning", "research", "question", "personal",
}


# ── Rule type ─────────────────────────────────────────────────────────────────

@dataclass
class TagRule:
    """A single tagging rule: a predicate over features → a set of tags."""
    name: str
    predicate: Callable[[MessageFeatures, str, str], bool]
    tags: List[str]
    confidence: float = 1.0


# ── Entity / keyword matchers ─────────────────────────────────────────────────

def _any_entity_match(features: MessageFeatures, terms: List[str]) -> bool:
    """True if any of `terms` appears (case-insensitive) in entities or keywords."""
    lowered = {e.lower() for e in features.entities} | {k.lower() for k in features.keywords}
    return any(t.lower() in lowered for t in terms)


def _text_contains_any(user_text: str, assistant_text: str, terms: List[str]) -> bool:
    """True if any term appears in the combined message text (case-insensitive)."""
    combined = (user_text + " " + assistant_text).lower()
    return any(t.lower() in combined for t in terms)


# ── Rule definitions ──────────────────────────────────────────────────────────

RULES: List[TagRule] = [

    # Code presence
    TagRule(
        name="code-block",
        predicate=lambda f, u, a: f.contains_code,
        tags=["code"],
    ),

    # Networking / infrastructure entities
    TagRule(
        name="networking-entities",
        predicate=lambda f, u, a: _any_entity_match(
            f, ["tailscale", "caddy", "nginx", "gateway", "vpn", "dns",
                "websocket", "tcp", "http", "ssl", "tls", "port", "firewall"]
        ) or _text_contains_any(u, a, ["tailscale", "caddy", "gateway", "vpn"]),
        tags=["networking", "infrastructure"],
    ),

    # Security topics
    TagRule(
        name="security",
        predicate=lambda f, u, a: _text_contains_any(
            u, a, ["security", "auth", "token", "credential", "allowlist",
                   "permission", "vulnerability", "cve", "exploit", "attack"]
        ),
        tags=["security"],
    ),

    # AI / LLM topics
    TagRule(
        name="ai-llm",
        predicate=lambda f, u, a: _text_contains_any(
            u, a, ["llm", "claude", "gpt", "anthropic", "openai", "model",
                   "prompt", "context", "embedding", "inference", "token"]
        ),
        tags=["ai", "llm"],
    ),

    # Context management (this project)
    TagRule(
        name="context-management",
        predicate=lambda f, u, a: _text_contains_any(
            u, a, ["context window", "compaction", "tagging", "tag-context",
                   "context management", "rl", "reinforcement learning",
                   "tagger", "quality agent", "dag"]
        ),
        tags=["context-management", "rl", "ai"],
    ),

    # Voice PWA
    TagRule(
        name="voice-pwa",
        predicate=lambda f, u, a: _text_contains_any(
            u, a, ["voice pwa", "voice-pwa", "push-to-talk", "whisper",
                   "speech", "tts", "voice backend", "voice frontend"]
        ),
        tags=["voice-pwa"],
    ),

    # OpenClaw system
    TagRule(
        name="openclaw",
        predicate=lambda f, u, a: _text_contains_any(
            u, a, ["openclaw", "claw", "heartbeat", "cron job", "session",
                   "workflow_auto", "compaction safeguard"]
        ),
        tags=["openclaw"],
    ),

    # Shopping list
    TagRule(
        name="shopping",
        predicate=lambda f, u, a: _text_contains_any(
            u, a, ["shopping", "grocery", "milk", "coffee", "shopping list",
                   "shopping bot", "devaul shop"]
        ),
        tags=["shopping-list"],
    ),

    # Deployment / devops
    TagRule(
        name="devops",
        predicate=lambda f, u, a: _text_contains_any(
            u, a, ["deploy", "launchd", "launchctl", "docker", "vercel",
                   "build", "npm run", "git push", "restart", "daemon"]
        ),
        tags=["devops", "deployment"],
    ),

    # URL present
    TagRule(
        name="contains-url",
        predicate=lambda f, u, a: f.contains_url,
        tags=["research"],
        confidence=0.5,
    ),

    # Question — user is asking for something
    TagRule(
        name="is-question",
        predicate=lambda f, u, a: f.is_question,
        tags=["question"],
        confidence=0.7,
    ),

    # Research / planning
    TagRule(
        name="research-planning",
        predicate=lambda f, u, a: _text_contains_any(
            u, a, ["research", "proposal", "design", "architecture", "plan",
                   "prototype", "spec", "document", "analysis"]
        ),
        tags=["research", "planning"],
    ),
]


# ── Tagger ────────────────────────────────────────────────────────────────────

@dataclass
class TagAssignment:
    """Result of a tagging operation."""
    tags: List[str]
    confidence: float          # average confidence of fired rules
    rules_fired: List[str]     # names of rules that matched


class StructuredProgramTagger:
    """
    v0 tagger: applies a fixed set of structured rules over MessageFeatures.

    This is the baseline "genome" — future GP-evolved taggers implement
    the same `assign()` interface.
    """

    def __init__(self, rules: Optional[List[TagRule]] = None,
                 min_confidence: float = 0.5) -> None:
        self._rules = rules if rules is not None else RULES
        self._min_confidence = min_confidence

    def assign(self, features: MessageFeatures,
               user_text: str, assistant_text: str) -> TagAssignment:
        """
        Run all rules against the features and texts.
        Returns deduplicated tags with aggregate confidence.
        """
        fired_tags: Set[str] = set()
        fired_rules: List[str] = []
        confidences: List[float] = []

        for rule in self._rules:
            if rule.confidence < self._min_confidence:
                continue
            try:
                if rule.predicate(features, user_text, assistant_text):
                    fired_tags.update(rule.tags)
                    fired_rules.append(rule.name)
                    confidences.append(rule.confidence)
            except Exception:
                pass  # individual rule failures are non-fatal

        avg_confidence = (sum(confidences) / len(confidences)) if confidences else 0.0

        # Canonicalize: only emit tags in CORE_TAGS (open extension possible later)
        canonical = [t for t in sorted(fired_tags) if t in CORE_TAGS]

        return TagAssignment(
            tags=canonical,
            confidence=avg_confidence,
            rules_fired=fired_rules,
        )


# ── Default instance ──────────────────────────────────────────────────────────

default_tagger = StructuredProgramTagger()


def assign_tags(features: MessageFeatures,
                user_text: str, assistant_text: str) -> List[str]:
    """Convenience function using the default tagger."""
    return default_tagger.assign(features, user_text, assistant_text).tags
