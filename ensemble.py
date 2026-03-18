"""
ensemble.py — Mixture model tagger combining multiple tagging strategies.

The ensemble runs multiple taggers on each message, combines their outputs
with quality-agent-derived weights, and applies a pruning step to maintain
precision while gaining recall from diversity.

This is the "tagger family" from the design doc.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from features import MessageFeatures
from tagger import TagAssignment, CORE_TAGS
from quality import QualityAgent
from tag_registry import get_registry


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
    Weighted mixture model over multiple taggers.

    Tags are included if their weighted vote exceeds the threshold.
    Weights come from the quality agent's historical fitness scores.

    This is the core "tagger family" from the tag-context design doc.
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
