"""
gp_tagger.py — Genetic programming harness for evolving tagging strategies.

Each evolved individual is a Boolean predicate tree over MessageFeatures.
One tree is evolved per tag in the vocabulary; together they form a GeneticTagger.

Fitness function: composite quality score from QualityAgent
  (context_density × 0.6 + (1 - reframing_rate) × 0.4)

The GeneticTagger is compatible with the StructuredProgramTagger interface:
  assign(features, user_text, assistant_text) -> TagAssignment

Evolution uses DEAP:
  - Ramped half-and-half tree initialisation
  - Tournament selection (size 3)
  - One-point crossover + subtree mutation
  - Hall-of-Fame tracks best individual per tag
"""

import functools
import operator
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from deap import algorithms, base, creator, gp, tools

from features import MessageFeatures, extract_features
from tagger import TagAssignment, CORE_TAGS
from reframing import reframing_rate

# ── Module-level helpers (must be picklable) ──────────────────────────────────

def _rand01() -> float:
    """Ephemeral constant generator: random float in [0, 1]."""
    return round(random.random(), 2)


def _not(a: float) -> float:
    return 1.0 - a


def _gt(a: float, t: float) -> float:
    return 1.0 if a > t else 0.0


# ── GP primitive set ──────────────────────────────────────────────────────────

def _make_pset() -> gp.PrimitiveSet:
    """
    Build the primitive set for tag predicate trees.

    Input features (in order):
      0: token_count (int, normalised 0-1)
      1: contains_code (bool as float)
      2: contains_url (bool as float)
      3: is_question (bool as float)
      4-13: entity_score_0..9 (float, 1.0 if entity[i] present, else 0.0)
      14-18: keyword_score_0..4 (float, TF-IDF-style, simplified)

    All primitives operate on floats; boolean semantics via threshold.
    """
    pset = gp.PrimitiveSet("PREDICATE", 34)

    # Rename arguments to meaningful names for inspection
    for i, name in enumerate([
        "token_count", "has_code", "has_url", "is_question",
        "ent0", "ent1", "ent2", "ent3", "ent4",
        "ent5", "ent6", "ent7", "ent8", "ent9",
    ] + [f"kw{j}" for j in range(20)]):
        pset.renameArguments(**{f"ARG{i}": name})

    # Logical ops (on floats: treat > 0.5 as True)
    pset.addPrimitive(operator.and_, 2, name="AND")     # min(a,b)
    pset.addPrimitive(operator.or_,  2, name="OR")      # max(a,b)
    pset.addPrimitive(_not, 1, name="NOT")

    # Arithmetic for combinations
    pset.addPrimitive(operator.add, 2, name="ADD")
    pset.addPrimitive(operator.mul, 2, name="MUL")

    # Threshold: > threshold → 1.0, else 0.0
    pset.addPrimitive(_gt, 2, name="GT")

    # Terminals: constants
    pset.addTerminal(0.0, name="ZERO")
    pset.addTerminal(0.5, name="HALF")
    pset.addTerminal(1.0, name="ONE")
    pset.addEphemeralConstant("rand01", _rand01)

    return pset


PSET = _make_pset()


# ── Feature vector extraction ─────────────────────────────────────────────────

# Entity and keyword vocabularies for the fixed GP input slots
_ENTITY_VOCAB = [
    "tailscale", "caddy", "gateway", "telegram", "openai", "anthropic",
    "vercel", "github", "docker", "sqlite",
]

_KEYWORD_VOCAB = [
    # Infrastructure / ops
    "security", "deployment", "running", "launchctl", "service",
    # Context / AI
    "context", "tagger", "model", "prompt", "tokens",
    # Voice PWA
    "voice", "pwa", "transcri",  # matches transcription/transcribe/transcribed
    "websocket", "audio",
    # Projects
    "shopping", "openclaw", "yapcad",
    # Research
    "corpus", "injection",
]


def features_to_vector(features: MessageFeatures,
                       user_text: str, assistant_text: str) -> List[float]:
    """Convert MessageFeatures to a fixed-length float vector for GP evaluation."""
    combined = (user_text + " " + assistant_text).lower()

    token_norm = min(1.0, features.token_count / 2000.0)
    has_code   = 1.0 if features.contains_code else 0.0
    has_url    = 1.0 if features.contains_url  else 0.0
    is_q       = 1.0 if features.is_question   else 0.0

    ent_scores = [
        1.0 if term in combined or any(term in e.lower() for e in features.entities)
        else 0.0
        for term in _ENTITY_VOCAB
    ]

    kw_scores = [
        1.0 if term in combined else 0.0
        for term in _KEYWORD_VOCAB
    ]

    return [token_norm, has_code, has_url, is_q] + ent_scores + kw_scores


# ── DEAP setup ────────────────────────────────────────────────────────────────

# Guard against re-registering creator classes on module reload
if not hasattr(creator, "FitnessMax"):
    creator.create("FitnessMax", base.Fitness, weights=(1.0,))
if not hasattr(creator, "Individual"):
    creator.create("Individual", gp.PrimitiveTree, fitness=creator.FitnessMax)


def _make_toolbox(pset: gp.PrimitiveSet) -> base.Toolbox:
    toolbox = base.Toolbox()
    toolbox.register("expr",       gp.genHalfAndHalf, pset=pset, min_=1, max_=4)
    toolbox.register("individual", tools.initIterate, creator.Individual, toolbox.expr)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)
    toolbox.register("compile",    gp.compile, pset=pset)
    toolbox.register("select",     tools.selTournament, tournsize=3)
    toolbox.register("mate",       gp.cxOnePoint)
    toolbox.register("expr_mut",   gp.genFull, min_=0, max_=2)
    toolbox.register("mutate",     gp.mutUniform, expr=toolbox.expr_mut, pset=pset)
    toolbox.decorate("mate",   gp.staticLimit(key=operator.attrgetter("height"), max_value=8))
    toolbox.decorate("mutate", gp.staticLimit(key=operator.attrgetter("height"), max_value=8))
    return toolbox


TOOLBOX = _make_toolbox(PSET)


# ── Predicate compilation ─────────────────────────────────────────────────────

def compile_predicate(individual: creator.Individual) -> Callable[[List[float]], float]:
    """Compile a GP individual into a callable: feature_vector → float (>0.5 = True)."""
    func = TOOLBOX.compile(expr=individual)
    return func


# ── Genetic Tagger ────────────────────────────────────────────────────────────

class TagPredictor:
    """A compiled predicate for one tag, with its source individual."""

    def __init__(self, tag: str, individual, predicate: Callable, fitness: float = 0.5):
        self.tag = tag
        self.individual = individual
        self._predicate = predicate
        self.fitness = fitness

    @property
    def predicate(self) -> Callable:
        return self._predicate

    def __getstate__(self):
        # Exclude compiled predicate — recompile on load
        return {"tag": self.tag, "individual": self.individual, "fitness": self.fitness}

    def __setstate__(self, state):
        self.tag = state["tag"]
        self.individual = state["individual"]
        self.fitness = state["fitness"]
        self._predicate = compile_predicate(self.individual)


@dataclass
class GeneticTagger:
    """
    A tagger whose rules are GP-evolved predicates.
    Compatible with StructuredProgramTagger.assign() interface.

    predictors: one TagPredictor per tag in the vocabulary
    tagger_id: unique ID for QualityAgent tracking
    """
    predictors: List[TagPredictor]
    tagger_id: str = field(default_factory=lambda: f"gp-{int(time.time())}")
    threshold: float = 0.5

    def assign(self, features: MessageFeatures,
               user_text: str, assistant_text: str) -> TagAssignment:
        """Run all evolved predicates and return fired tags."""
        vec = features_to_vector(features, user_text, assistant_text)
        tags = []
        rules_fired = []
        confidences = []

        for pred in self.predictors:
            try:
                score = float(pred.predicate(*vec))
                if score > self.threshold:
                    tags.append(pred.tag)
                    rules_fired.append(f"gp:{pred.tag}")
                    confidences.append(min(1.0, score))
            except Exception:
                pass

        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        return TagAssignment(
            tags=sorted(tags),
            confidence=avg_conf,
            rules_fired=rules_fired,
        )


# ── Evolution ─────────────────────────────────────────────────────────────────

def _evaluate_individual(individual, tag: str,
                         training_examples: List[Tuple[List[float], bool]],
                         penalty: float = 0.005) -> Tuple[float]:
    """
    Fitness function for a single tag's predicate.

    training_examples: [(feature_vector, should_have_tag)]
    Fitness = balanced_accuracy - size_penalty

    Balanced accuracy = (TPR + TNR) / 2, which prevents degenerate
    always-True or always-False solutions from scoring well on
    imbalanced datasets.
    """
    try:
        func = TOOLBOX.compile(expr=individual)
    except Exception:
        return (0.0,)

    if not training_examples:
        return (0.5,)

    tp = fp = tn = fn = 0
    for vec, label in training_examples:
        try:
            pred = float(func(*vec)) > 0.5
            if label:
                if pred: tp += 1
                else:    fn += 1
            else:
                if pred: fp += 1
                else:    tn += 1
        except Exception:
            fn += 1 if label else (fp := fp)  # penalise crashes

    tpr = tp / (tp + fn) if (tp + fn) > 0 else 0.0  # sensitivity
    tnr = tn / (tn + fp) if (tn + fp) > 0 else 0.0  # specificity
    balanced_acc = (tpr + tnr) / 2.0

    size_penalty = penalty * len(individual)
    return (max(0.0, balanced_acc - size_penalty),)


def evolve_predicates_for_tag(
    tag: str,
    training_examples: List[Tuple[List[float], bool]],
    pop_size: int = 50,
    n_gen: int = 20,
    verbose: bool = False,
) -> TagPredictor:
    """
    Evolve a Boolean predicate for one tag.

    training_examples: list of (feature_vector, True/False) — does this
    message/response pair warrant the tag?
    """
    toolbox = base.Toolbox()
    toolbox.register("expr",       gp.genHalfAndHalf, pset=PSET, min_=1, max_=4)
    toolbox.register("individual", tools.initIterate, creator.Individual, toolbox.expr)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)
    toolbox.register("compile",    gp.compile, pset=PSET)
    toolbox.register("evaluate",   _evaluate_individual,
                     tag=tag, training_examples=training_examples)
    toolbox.register("select",     tools.selTournament, tournsize=3)
    toolbox.register("mate",       gp.cxOnePoint)
    toolbox.register("expr_mut",   gp.genFull, min_=0, max_=2)
    toolbox.register("mutate",     gp.mutUniform, expr=toolbox.expr_mut, pset=PSET)
    toolbox.decorate("mate",   gp.staticLimit(key=operator.attrgetter("height"), max_value=8))
    toolbox.decorate("mutate", gp.staticLimit(key=operator.attrgetter("height"), max_value=8))

    pop = toolbox.population(n=pop_size)
    hof = tools.HallOfFame(1)
    stats = tools.Statistics(lambda ind: ind.fitness.values)
    stats.register("max", max)

    algorithms.eaSimple(
        pop, toolbox,
        cxpb=0.7, mutpb=0.3,
        ngen=n_gen,
        stats=stats if verbose else None,
        halloffame=hof,
        verbose=verbose,
    )

    best = hof[0]
    fitness_val = best.fitness.values[0] if best.fitness.valid else 0.5
    predicate = compile_predicate(best)

    return TagPredictor(tag=tag, individual=best,
                        predicate=predicate, fitness=fitness_val)


def build_training_examples(
    records,  # Iterable[InteractionRecord] from logger
    tag: str,
    baseline_assign,  # callable: (features, u, a) -> List[str]
) -> List[Tuple[List[float], bool]]:
    """
    Build training examples for a tag by labelling records with the baseline tagger.

    Uses baseline tagger labels as pseudo-ground-truth for initial evolution.
    In v0.3, this would be replaced by quality-agent-derived labels.
    """
    examples = []
    for record in records:
        features = extract_features(record.user_text, record.assistant_text)
        baseline_tags = baseline_assign(features, record.user_text, record.assistant_text)
        label = tag in baseline_tags
        vec = features_to_vector(features, record.user_text, record.assistant_text)
        examples.append((vec, label))
    return examples


def evolve_genetic_tagger(
    records,             # Iterable[InteractionRecord]
    tags: Optional[List[str]] = None,
    pop_size: int = 50,
    n_gen: int = 20,
    verbose: bool = False,
) -> GeneticTagger:
    """
    Evolve a complete GeneticTagger — one predicate per tag.

    Parameters
    ----------
    records     Interaction records to use as training data.
    tags        Tags to evolve (defaults to CORE_TAGS).
    pop_size    GP population size per tag.
    n_gen       Generations per tag.
    verbose     Print evolution stats.
    """
    from tagger import assign_tags as baseline_assign

    records = list(records)  # materialise
    tag_vocab = sorted(tags or CORE_TAGS)
    predictors = []

    for tag in tag_vocab:
        if verbose:
            print(f"  Evolving predicate for tag: {tag!r}")
        examples = build_training_examples(records, tag, baseline_assign)
        predictor = evolve_predicates_for_tag(
            tag, examples,
            pop_size=pop_size, n_gen=n_gen, verbose=verbose,
        )
        predictors.append(predictor)
        if verbose:
            print(f"    → fitness={predictor.fitness:.3f}  "
                  f"tree_size={len(predictor.individual)}")

    return GeneticTagger(predictors=predictors)
