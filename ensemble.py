"""
ensemble.py — Thin wrapper around FixedTagger for the tag-context system.

Originally designed as a mixture model combining multiple tagging strategies
(Fixed + GP tagger), but the GP tagger requires the DEAP library which
was never successfully integrated. This module now serves as a compatibility
wrapper around FixedTagger.

The "hybrid" and "gp-only" modes are deprecated — they will raise ImportError
if DEAP is not installed. Use TAGGER_MODE="fixed" (the default).
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from features import MessageFeatures
from tagger import TagAssignment, CORE_TAGS
from quality import QualityAgent
from tag_registry import get_registry
from fixed_tagger import FixedTagger
import config


@dataclass
class TaggerEntry:
    """A registered tagger in the ensemble."""
    tagger_id: str
    assign_fn: Callable[[MessageFeatures, str, str], TagAssignment]
    weight: float = 1.0   # updated by quality agent scores


@dataclass
class EnsembleResult:
    """Result of ensemble tagging with per-tagger attribution."""
    tags: List[str]
    confidence: float
    per_tagger: Dict[str, List[str]]  # tagger_id → tags it contributed
    tag_votes: Dict[str, float]       # tag → weighted vote score


class EnsembleTagger:
    """
    FixedTagger-compatible wrapper (formerly a multi-tagger ensemble).

    Originally designed to run multiple taggers with weighted voting, but
    since the GP tagger (DEAP-based) never worked, this now effectively
    wraps a single FixedTagger. The ensemble infrastructure (weight updates,
    per-tagger attribution, vote thresholds) is preserved for future use
    if additional taggers are added.

    Tags are included if their weighted vote exceeds the threshold.
    Weights come from the quality agent's historical fitness scores.
    """

    def __init__(
        self,
        quality_agent: Optional[QualityAgent] = None,
        vote_threshold: float = 0.4,
        min_weight: float = 0.1,
    ) -> None:
        self._taggers: List[TaggerEntry] = []
        self._qa = quality_agent
        self._vote_threshold = vote_threshold
        self._min_weight = min_weight

    def register(self, tagger_id: str,
                 assign_fn: Callable[[MessageFeatures, str, str], TagAssignment],
                 initial_weight: float = 1.0) -> None:
        """Add a tagger to the ensemble."""
        self._taggers.append(TaggerEntry(
            tagger_id=tagger_id,
            assign_fn=assign_fn,
            weight=initial_weight,
        ))

    def update_weights(self, last_n: int = 20) -> None:
        """Update tagger weights from quality agent scores."""
        if not self._qa:
            return
        for entry in self._taggers:
            fitness = self._qa.fitness(entry.tagger_id, last_n)
            entry.weight = max(self._min_weight, fitness)

    def assign(self, features: MessageFeatures,
               user_text: str, assistant_text: str) -> EnsembleResult:
        """
        Run all taggers, aggregate via weighted vote, prune low-confidence tags.
        """
        if not self._taggers:
            return EnsembleResult(
                tags=[], confidence=0.0, per_tagger={}, tag_votes={},
            )

        # Collect results from all taggers
        per_tagger: Dict[str, List[str]] = {}
        tag_votes: Dict[str, float] = {}
        total_weight = sum(e.weight for e in self._taggers)

        for entry in self._taggers:
            try:
                result = entry.assign_fn(features, user_text, assistant_text)
                tags = result.tags if isinstance(result, TagAssignment) else list(result)
            except Exception:
                tags = []

            per_tagger[entry.tagger_id] = tags
            normalised_weight = entry.weight / total_weight if total_weight > 0 else 0

            for tag in tags:
                tag_votes[tag] = tag_votes.get(tag, 0.0) + normalised_weight

        # Threshold filter: only tags with sufficient weighted support
        # Use registry to get active tags (core + candidate)
        registry = get_registry()
        active_tags = registry.get_active_tags()

        accepted_tags = sorted(
            tag for tag, vote in tag_votes.items()
            if vote >= self._vote_threshold and tag in active_tags
        )

        # Aggregate confidence: mean vote score of accepted tags
        if accepted_tags:
            confidence = sum(tag_votes[t] for t in accepted_tags) / len(accepted_tags)
        else:
            confidence = 0.0

        return EnsembleResult(
            tags=accepted_tags,
            confidence=confidence,
            per_tagger=per_tagger,
            tag_votes=tag_votes,
        )

    def explain(self, result: EnsembleResult) -> str:
        """Return a human-readable explanation of an ensemble tagging."""
        lines = [f"Ensemble: {len(result.tags)} tags accepted (threshold={self._vote_threshold})"]
        for tag in result.tags:
            vote = result.tag_votes.get(tag, 0)
            sources = [tid for tid, tags in result.per_tagger.items() if tag in tags]
            lines.append(f"  {tag:<25} vote={vote:.2f}  from: {', '.join(sources)}")
        rejected = sorted(t for t, v in result.tag_votes.items()
                         if t not in result.tags and v > 0)
        if rejected:
            lines.append("  Pruned (below threshold):")
            for tag in rejected:
                lines.append(f"    {tag:<23} vote={result.tag_votes[tag]:.2f}")
        return "\n".join(lines)


def build_ensemble(
    mode: Optional[str] = None,
    quality_agent: Optional[QualityAgent] = None,
    vote_threshold: float = 0.4,
) -> EnsembleTagger:
    """
    Build a tagger based on the specified mode.

    Modes:
    - "fixed": FixedTagger only (default — recommended, no extra deps)
    - "hybrid": DEPRECATED — FixedTagger + GP tagger (requires DEAP)
    - "gp-only": DEPRECATED — GP tagger only (requires DEAP)

    If mode is None, uses config.TAGGER_MODE (defaults to "fixed").
    """
    mode = mode or config.TAGGER_MODE
    ensemble = EnsembleTagger(
        quality_agent=quality_agent,
        vote_threshold=vote_threshold,
    )

    if mode == "fixed":
        # Fixed tagger only — no DEAP dependency required
        fixed = FixedTagger(config.TAGS_CONFIG)
        ensemble.register("fixed", fixed.assign, initial_weight=1.0)

    elif mode == "hybrid":
        # Fixed + GP tagger
        fixed = FixedTagger(config.TAGS_CONFIG)
        ensemble.register("fixed", fixed.assign, initial_weight=1.0)

        # Import GP tagger only when needed
        try:
            from gp_tagger import StructuredProgramTagger
            gp_tagger = StructuredProgramTagger()
            ensemble.register("gp", gp_tagger.assign, initial_weight=1.0)
        except ImportError as e:
            raise ImportError(
                f"GP tagger requires DEAP: pip install deap (error: {e})"
            )

    elif mode == "gp-only":
        # Legacy mode: GP tagger only
        try:
            from gp_tagger import StructuredProgramTagger
            gp_tagger = StructuredProgramTagger()
            ensemble.register("gp", gp_tagger.assign, initial_weight=1.0)
        except ImportError as e:
            raise ImportError(
                f"GP tagger requires DEAP: pip install deap (error: {e})"
            )

    else:
        raise ValueError(f"Unknown tagger mode: {mode}. Use 'fixed', 'hybrid', or 'gp-only'.")

    return ensemble
