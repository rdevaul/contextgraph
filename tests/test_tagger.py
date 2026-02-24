"""Tests for tagger.py"""
from features import extract_features
from tagger import assign_tags, StructuredProgramTagger, RULES


def test_code_tag():
    tags = assign_tags(
        extract_features("how do I fix this?", "```python\nprint('hello')\n```"),
        "how do I fix this?", "```python\nprint('hello')\n```"
    )
    assert "code" in tags


def test_security_tag():
    tags = assign_tags(
        extract_features("can you check the security token?", "sure"),
        "can you check the security token?", "sure"
    )
    assert "security" in tags


def test_networking_tag():
    tags = assign_tags(
        extract_features("tailscale is showing offline", "let me check the gateway"),
        "tailscale is showing offline", "let me check the gateway"
    )
    assert "networking" in tags


def test_context_management_tag():
    tags = assign_tags(
        extract_features("the context window keeps filling up with unrelated stuff",
                         "yes, compaction blends unrelated topics"),
        "the context window keeps filling up", "yes, compaction blends topics"
    )
    assert "context-management" in tags


def test_no_spurious_tags_on_plain_text():
    tags = assign_tags(
        extract_features("what's for dinner?", "pasta sounds good"),
        "what's for dinner?", "pasta sounds good"
    )
    # Should get 'question' at most, not infrastructure/security tags
    assert "networking" not in tags
    assert "security" not in tags
    assert "code" not in tags


def test_tagger_interface():
    """StructuredProgramTagger.assign returns TagAssignment with correct fields."""
    from tagger import StructuredProgramTagger
    from features import extract_features
    tagger = StructuredProgramTagger()
    f = extract_features("deploy to vercel", "pushed to main branch")
    result = tagger.assign(f, "deploy to vercel", "pushed to main branch")
    assert isinstance(result.tags, list)
    assert isinstance(result.confidence, float)
    assert isinstance(result.rules_fired, list)
