"""Tests for ensemble.py"""
from features import extract_features, MessageFeatures
from tagger import TagAssignment, assign_tags
from ensemble import EnsembleTagger, EnsembleResult


def _make_tagger_fn(fixed_tags):
    """Return a mock tagger that always returns fixed_tags."""
    def assign(features, user_text, assistant_text):
        return TagAssignment(tags=fixed_tags, confidence=1.0, rules_fired=[])
    return assign


def test_ensemble_single_tagger():
    ens = EnsembleTagger(vote_threshold=0.3)
    ens.register("a", _make_tagger_fn(["security", "networking"]))
    f = extract_features("test", "test")
    result = ens.assign(f, "test", "test")
    assert "security" in result.tags
    assert "networking" in result.tags


def test_ensemble_weighted_vote():
    ens = EnsembleTagger(vote_threshold=0.5)
    ens.register("a", _make_tagger_fn(["security", "networking"]), initial_weight=2.0)
    ens.register("b", _make_tagger_fn(["security"]), initial_weight=1.0)
    f = extract_features("test", "test")
    result = ens.assign(f, "test", "test")
    assert "security" in result.tags  # both agree
    # networking only from tagger a (weight 2/3 = 0.67 > threshold)
    assert "networking" in result.tags


def test_ensemble_prunes_low_vote():
    ens = EnsembleTagger(vote_threshold=0.5)
    ens.register("a", _make_tagger_fn(["security"]), initial_weight=1.0)
    ens.register("b", _make_tagger_fn(["code"]), initial_weight=1.0)
    ens.register("c", _make_tagger_fn([]), initial_weight=1.0)
    f = extract_features("test", "test")
    result = ens.assign(f, "test", "test")
    # security: 1/3 = 0.33 < 0.5 → pruned
    assert "security" not in result.tags
    assert "code" not in result.tags


def test_ensemble_explain():
    ens = EnsembleTagger(vote_threshold=0.3)
    ens.register("baseline", _make_tagger_fn(["security", "networking"]))
    ens.register("gp", _make_tagger_fn(["security"]))
    f = extract_features("test", "test")
    result = ens.assign(f, "test", "test")
    explanation = ens.explain(result)
    assert "security" in explanation
    assert "baseline" in explanation


def test_ensemble_empty():
    ens = EnsembleTagger()
    f = extract_features("test", "test")
    result = ens.assign(f, "test", "test")
    assert result.tags == []
