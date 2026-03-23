"""
test_fixed_tagger.py — Tests for FixedTagger YAML-based tag assignment.

Tests:
- YAML loading
- Keyword matching (word-boundary)
- Pattern matching (regex)
- Hot-reload detection
- requires_all logic
- enabled/disabled tags
"""

import time
from pathlib import Path
import pytest

from fixed_tagger import FixedTagger
from features import MessageFeatures


def make_features() -> MessageFeatures:
    """Helper to create a default MessageFeatures instance."""
    return MessageFeatures(
        token_count=50,
        entities=[],
        noun_phrases=[],
        contains_code=False,
        contains_url=False,
        is_question=False,
        keywords=[]
    )


def test_yaml_loading(tmp_path):
    """Test basic YAML loading and tag extraction."""
    config_path = tmp_path / "tags.yaml"
    config_path.write_text("""
version: 1
tags:
  - name: test-tag
    description: A test tag
    keywords:
      - keyword1
      - keyword2
    confidence: 1.0
""")

    tagger = FixedTagger(config_path)
    assert "test-tag" in tagger.tag_names


def test_keyword_matching_word_boundary(tmp_path):
    """Test keyword matching with word boundaries."""
    config_path = tmp_path / "tags.yaml"
    config_path.write_text("""
version: 1
tags:
  - name: deployment
    description: Deployment discussions
    keywords:
      - deploy
      - deployment
    confidence: 1.0
""")

    tagger = FixedTagger(config_path)
    features = make_features()

    # Should match
    result = tagger.assign(features, "How do I deploy to production?", "Use the deploy script")
    assert "deployment" in result.tags

    # Should NOT match partial word (word boundary)
    result = tagger.assign(features, "I need to redeploy the app", "")
    assert "deployment" not in result.tags  # "redeploy" doesn't match "deploy" keyword


def test_pattern_matching(tmp_path):
    """Test regex pattern matching."""
    config_path = tmp_path / "tags.yaml"
    config_path.write_text("""
version: 1
tags:
  - name: code
    description: Messages with code blocks
    keywords: []
    patterns:
      - "```[\\\\s\\\\S]*?```"
    confidence: 1.0
""")

    tagger = FixedTagger(config_path)
    features = make_features()

    # Should match code block
    result = tagger.assign(
        features,
        "Here's the fix:",
        "```python\\nprint('hello')\\n```"
    )
    assert "code" in result.tags

    # Should NOT match without code block
    result = tagger.assign(features, "No code here", "Just text")
    assert "code" not in result.tags


def test_requires_all_logic(tmp_path):
    """Test requires_all=true tag matching."""
    config_path = tmp_path / "tags.yaml"
    config_path.write_text("""
version: 1
tags:
  - name: security-vuln
    description: Security vulnerability discussions
    keywords:
      - security
      - vulnerability
    requires_all: true
    confidence: 1.0
""")

    tagger = FixedTagger(config_path)
    features = make_features()

    # Should match when ALL keywords present
    result = tagger.assign(
        features,
        "Found a security vulnerability",
        "Please patch immediately"
    )
    assert "security-vuln" in result.tags

    # Should NOT match when only one keyword present
    result = tagger.assign(
        features,
        "Security best practices",
        "Follow the guide"
    )
    assert "security-vuln" not in result.tags


def test_disabled_tags(tmp_path):
    """Test that disabled tags are not loaded."""
    config_path = tmp_path / "tags.yaml"
    config_path.write_text("""
version: 1
tags:
  - name: active-tag
    description: Active tag
    keywords:
      - active
    confidence: 1.0

  - name: disabled-tag
    description: Disabled tag
    enabled: false
    keywords:
      - disabled
    confidence: 1.0
""")

    tagger = FixedTagger(config_path)

    assert "active-tag" in tagger.tag_names
    assert "disabled-tag" not in tagger.tag_names


def test_hot_reload(tmp_path):
    """Test that config changes are detected and reloaded."""
    config_path = tmp_path / "tags.yaml"

    # Initial config
    config_path.write_text("""
version: 1
tags:
  - name: tag1
    description: First tag
    keywords:
      - keyword1
    confidence: 1.0
""")

    tagger = FixedTagger(config_path)
    assert "tag1" in tagger.tag_names

    # Wait a bit to ensure mtime changes
    time.sleep(0.1)

    # Update config
    config_path.write_text("""
version: 1
tags:
  - name: tag1
    description: First tag
    keywords:
      - keyword1
    confidence: 1.0

  - name: tag2
    description: Second tag
    keywords:
      - keyword2
    confidence: 1.0
""")

    # Trigger reload by calling assign
    features = make_features()
    result = tagger.assign(features, "test keyword2", "")

    # Should now include tag2
    assert "tag2" in result.tags
    assert "tag2" in tagger.tag_names


def test_confidence_scores(tmp_path):
    """Test that custom confidence scores are respected."""
    config_path = tmp_path / "tags.yaml"
    config_path.write_text("""
version: 1
tags:
  - name: high-conf
    description: High confidence tag
    keywords:
      - certain
    confidence: 0.95

  - name: low-conf
    description: Low confidence tag
    keywords:
      - maybe
    confidence: 0.3
""")

    tagger = FixedTagger(config_path)
    features = make_features()

    # Test high confidence
    result = tagger.assign(features, "This is certain", "")
    assert "high-conf" in result.tags
    assert result.confidence == 0.95

    # Test low confidence
    result = tagger.assign(features, "Maybe this works", "")
    assert "low-conf" in result.tags
    assert result.confidence == 0.3


def test_case_insensitive_matching(tmp_path):
    """Test that keyword matching is case-insensitive."""
    config_path = tmp_path / "tags.yaml"
    config_path.write_text("""
version: 1
tags:
  - name: ai-tag
    description: AI discussions
    keywords:
      - claude ai
      - chatgpt
    confidence: 1.0
""")

    tagger = FixedTagger(config_path)
    features = make_features()

    # Should match different cases
    result = tagger.assign(features, "I'm using CLAUDE AI", "")
    assert "ai-tag" in result.tags

    result = tagger.assign(features, "ChatGPT is helpful", "")
    assert "ai-tag" in result.tags


def test_multiple_tags_single_message(tmp_path):
    """Test that multiple tags can be assigned to a single message."""
    config_path = tmp_path / "tags.yaml"
    config_path.write_text("""
version: 1
tags:
  - name: networking
    description: Networking
    keywords:
      - nginx
      - caddy
    confidence: 1.0

  - name: devops
    description: DevOps
    keywords:
      - deploy
      - docker
    confidence: 1.0
""")

    tagger = FixedTagger(config_path)
    features = make_features()

    result = tagger.assign(
        features,
        "How do I deploy nginx with docker?",
        "Use docker-compose"
    )

    assert "networking" in result.tags
    assert "devops" in result.tags
    assert len(result.tags) == 2


def test_empty_result(tmp_path):
    """Test that no tags are assigned when nothing matches."""
    config_path = tmp_path / "tags.yaml"
    config_path.write_text("""
version: 1
tags:
  - name: specific-tag
    description: Very specific
    keywords:
      - veryrareword
    confidence: 1.0
""")

    tagger = FixedTagger(config_path)
    features = make_features()

    result = tagger.assign(features, "Common words here", "Nothing special")

    assert len(result.tags) == 0
    assert result.confidence == 0.0
