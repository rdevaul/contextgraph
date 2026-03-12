"""
shadow.py — Phase 2: Shadow mode evaluation.

For each interaction in the store, simulate context assembly as if the
graph had been providing context. Score with the quality agent. Log results
without changing the actual user experience.

This answers the key question: does graph assembly produce *better* context
than a recency-only linear window?

Usage:
  python3 scripts/shadow.py [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                             [--budget 4000] [--verbose] [--report]

Output:
  - data/shadow-log.jsonl     — per-interaction shadow assembly records
  - data/shadow-report.json   — aggregate metrics
  - quality-state.json        — updated quality agent scores
"""

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from assembler import ContextAssembler, AssemblyResult
from ensemble import EnsembleTagger
from features import extract_features
from gp_tagger import GeneticTagger
from logger import iter_records
from quality import QualityAgent
from reframing import detect_reframing, is_system_artifact
from store import MessageStore
from tagger import assign_tags


SHADOW_LOG = Path(__file__).parent.parent / "data" / "shadow-log.jsonl"
SHADOW_REPORT = Path(__file__).parent.parent / "data" / "shadow-report.json"
GP_TAGGER_PATH = Path(__file__).parent.parent / "data" / "gp-tagger.pkl"
DEFAULT_DB = Path.home() / ".tag-context" / "store.db"


def build_ensemble(verbose: bool = False) -> EnsembleTagger:
    """Build the standard ensemble tagger (baseline + GP)."""
    ensemble = EnsembleTagger(vote_threshold=0.3)
    ensemble.register("v0-baseline", assign_tags, initial_weight=1.0)

    if GP_TAGGER_PATH.exists():
        with GP_TAGGER_PATH.open("rb") as f:
            gp_tagger = pickle.load(f)
        ensemble.register(gp_tagger.tagger_id, gp_tagger.assign, initial_weight=0.8)
        if verbose:
            print(f"Ensemble: baseline + GP ({gp_tagger.tagger_id})")
    else:
        if verbose:
            print("Ensemble: baseline only")

    return ensemble


def simulate_linear_window(store: MessageStore, current_ts: float,
                           token_budget: int) -> dict:
    """
    Simulate a linear recency-only window *as of* current_ts (the baseline to beat).

    Retrieves all messages prior to current_ts, sorts newest-first, and packs
    to the token budget — exactly what a sliding window would have provided.
    """
    # Pull all messages in the store that predate this interaction
    # get_by_tag with a broad query isn't available, so use get_recent with
    # a large N and filter by timestamp
    all_prior = [m for m in store.get_recent(2000) if m.timestamp < current_ts]
    # Already newest-first from get_recent
    tokens = 0
    packed = []
    for msg in all_prior:
        cost = msg.token_count if msg.token_count > 0 else \
            max(1, int(len((msg.user_text + " " + msg.assistant_text).split()) * 1.3))
        if tokens + cost > token_budget:
            break
        packed.append(msg)
        tokens += cost

    return {
        "message_count": len(packed),
        "total_tokens": tokens,
        "unique_tags": sorted(set(tag for m in packed for tag in m.tags)),
        "tag_count": len(set(tag for m in packed for tag in m.tags)),
    }


def run_shadow(db_path: str, start_date: str | None, end_date: str | None,
               token_budget: int, verbose: bool) -> dict:
    """
    Run shadow evaluation over all interactions in the specified window.

    For each interaction:
    1. Infer tags for the user message
    2. Run the context assembler (graph-based)
    3. Simulate what a linear window would have provided
    4. Score with quality agent
    5. Log the comparison

    Returns aggregate metrics.
    """
    store = MessageStore(db_path=db_path)
    ensemble = build_ensemble(verbose=verbose)
    assembler = ContextAssembler(store, token_budget=token_budget)
    quality = QualityAgent()

    records = list(iter_records(start_date=start_date, end_date=end_date))
    if not records:
        print("No interaction records found.", file=sys.stderr)
        return {}

    # Collect recent user texts for reframing window
    user_text_window: list[str] = []
    REFRAME_WINDOW = 10

    # Shadow log
    shadow_entries: list[dict] = []

    # Aggregate metrics
    metrics = {
        "total_interactions": 0,
        "graph_topic_retrievals": 0,      # interactions where graph added topic context
        "graph_mean_density": 0.0,
        "graph_mean_topic_count": 0.0,
        "graph_mean_total": 0.0,
        "linear_mean_count": 0.0,
        "linear_mean_tokens": 0.0,
        "graph_mean_tokens": 0.0,
        "novel_from_graph_total": 0,
        "reframing_total": 0,
        "reframing_rate": 0.0,
        "graph_unique_tags_surfaced": set(),
        "quality_scores": [],
    }

    print(f"Shadow evaluation: {len(records)} interactions, budget={token_budget} tokens")
    print()

    for i, record in enumerate(records):
        # Skip very short exchanges and system artifacts
        if len(record.user_text.strip()) < 10:
            continue
        if is_system_artifact(record.user_text):
            continue

        metrics["total_interactions"] += 1

        # 1. Infer tags
        features = extract_features(record.user_text, record.assistant_text)
        ens_result = ensemble.assign(features, record.user_text, record.assistant_text)
        inferred_tags = ens_result.tags

        # 2. Graph-based assembly
        assembly = assembler.assemble(record.user_text, inferred_tags)

        # 3. Linear baseline simulation (as of this interaction's timestamp)
        linear = simulate_linear_window(store, record.interaction_at, token_budget)

        # 4. Reframing detection
        reframe = detect_reframing(record.user_text)
        user_text_window.append(record.user_text)
        if len(user_text_window) > REFRAME_WINDOW:
            user_text_window = user_text_window[-REFRAME_WINDOW:]

        # 5. Quality agent scoring
        quality_score = quality.record(
            tagger_id="ensemble-shadow",
            assembly_result=assembly,
            user_text=record.user_text,
            recent_user_texts=user_text_window,
        )

        # 6. Compute graph vs. linear comparison
        # How many graph messages would NOT have been in the linear window?
        linear_msg_count = linear["message_count"]
        graph_total = len(assembly.messages)
        # Novel context = topic-retrieved messages (recency layer overlaps with linear)
        novel_from_graph = assembly.topic_count
        # Tag diversity: graph can surface older, on-topic messages from many tags
        graph_tag_count = len(set(assembly.tags_used))
        linear_tag_count = linear["tag_count"]

        # Log entry
        entry = {
            "timestamp": record.interaction_at,
            "interaction_id": record.id,
            "user_text_preview": record.user_text[:100],
            "inferred_tags": inferred_tags,
            "graph": {
                "total_messages": graph_total,
                "recency_count": assembly.recency_count,
                "topic_count": assembly.topic_count,
                "total_tokens": assembly.total_tokens,
                "tags_used": assembly.tags_used,
                "tag_count": graph_tag_count,
            },
            "linear": linear,
            "comparison": {
                "novel_from_graph": novel_from_graph,
                "graph_tag_diversity": graph_tag_count,
                "linear_tag_diversity": linear_tag_count,
                "graph_advantage_msgs": graph_total - linear_msg_count,
            },
            "reframing": {
                "is_reframing": reframe.is_reframing,
                "confidence": reframe.confidence,
                "signals": reframe.signals_found,
            },
            "quality": {
                "density": quality_score.context_density,
                "reframing_signal": quality_score.reframing_signal,
                "composite": quality_score.composite,
            },
        }
        shadow_entries.append(entry)

        # Accumulate metrics
        if assembly.topic_count > 0:
            metrics["graph_topic_retrievals"] += 1
        metrics["graph_mean_density"] += quality_score.context_density
        metrics["graph_mean_topic_count"] += assembly.topic_count
        metrics["graph_mean_total"] += len(assembly.messages)
        metrics["graph_mean_tokens"] += assembly.total_tokens
        metrics["linear_mean_count"] += linear["message_count"]
        metrics["linear_mean_tokens"] += linear["total_tokens"]
        metrics["novel_from_graph_total"] += novel_from_graph
        if reframe.is_reframing:
            metrics["reframing_total"] += 1
        metrics["graph_unique_tags_surfaced"].update(inferred_tags)
        metrics["quality_scores"].append(quality_score.composite)

        if verbose:
            ts = time.strftime("%m-%d %H:%M", time.localtime(record.interaction_at))
            tags_str = ",".join(inferred_tags[:3]) or "(none)"
            print(f"  [{ts}] tags=[{tags_str}] graph={assembly.topic_count}+{assembly.recency_count} "
                  f"linear={linear['message_count']} density={quality_score.context_density:.2f} "
                  f"reframe={'Y' if reframe.is_reframing else 'n'}")

    # Finalize metrics
    n = metrics["total_interactions"]
    if n > 0:
        metrics["graph_mean_density"] /= n
        metrics["graph_mean_topic_count"] /= n
        metrics["graph_mean_total"] /= n
        metrics["graph_mean_tokens"] /= n
        metrics["linear_mean_count"] /= n
        metrics["linear_mean_tokens"] /= n
        metrics["reframing_rate"] = metrics["reframing_total"] / n
        metrics["quality_mean_composite"] = sum(metrics["quality_scores"]) / n
    metrics["graph_unique_tags_surfaced"] = sorted(metrics["graph_unique_tags_surfaced"])

    # Write shadow log
    SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SHADOW_LOG.open("w") as f:
        for entry in shadow_entries:
            f.write(json.dumps(entry) + "\n")

    # Write report
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "token_budget": token_budget,
        "total_interactions": n,
        "graph": {
            "topic_retrieval_rate": round(metrics["graph_topic_retrievals"] / n, 4) if n else 0,
            "mean_density": round(metrics["graph_mean_density"], 4),
            "mean_topic_msgs": round(metrics["graph_mean_topic_count"], 2),
            "mean_total_msgs": round(metrics["graph_mean_total"], 2),
            "mean_tokens": round(metrics["graph_mean_tokens"], 0),
            "novel_msgs_total": metrics["novel_from_graph_total"],
            "unique_tags_surfaced": metrics["graph_unique_tags_surfaced"],
        },
        "linear": {
            "mean_msgs": round(metrics["linear_mean_count"], 2),
            "mean_tokens": round(metrics["linear_mean_tokens"], 0),
        },
        "reframing_rate": round(metrics["reframing_rate"], 4),
        "quality_mean_composite": round(metrics.get("quality_mean_composite", 0), 4),
        "success_criteria": {
            "reframing_rate_target": "< 0.05",
            "reframing_rate_actual": round(metrics["reframing_rate"], 4),
            "reframing_pass": metrics["reframing_rate"] < 0.05,
            "density_target": "> 0.60",
            "density_actual": round(metrics["graph_mean_density"], 4),
            "density_pass": metrics["graph_mean_density"] > 0.60,
        },
    }
    with SHADOW_REPORT.open("w") as f:
        json.dump(report, f, indent=2)

    # Remove non-serializable keys for return
    del metrics["quality_scores"]
    return report


def print_report(report: dict) -> None:
    """Pretty-print the shadow report."""
    g = report["graph"]
    l = report["linear"]
    sc = report["success_criteria"]

    print("\n" + "=" * 62)
    print("  SHADOW MODE REPORT — Phase 2 Context Graph Evaluation")
    print("=" * 62)
    print(f"  Generated:       {report['generated_at']}")
    print(f"  Interactions:    {report['total_interactions']}")
    print(f"  Token budget:    {report['token_budget']}")
    print()

    print("  ── Graph Assembly ──────────────────────────────────────")
    print(f"  Topic retrieval rate:   {g['topic_retrieval_rate']:.1%}")
    print(f"  Mean density:           {g['mean_density']:.1%}   (topic / total assembled)")
    print(f"  Mean total messages:    {g['mean_total_msgs']:.1f}")
    print(f"  Mean topic messages:    {g['mean_topic_msgs']:.1f}   (above linear recency)")
    print(f"  Mean tokens used:       {g['mean_tokens']:.0f}")
    print(f"  Novel msgs delivered:   {g['novel_msgs_total']} total across all queries")
    print(f"  Tags surfaced:          {len(g['unique_tags_surfaced'])}")
    print()

    print("  ── Linear Baseline ─────────────────────────────────────")
    print(f"  Mean messages/window:   {l['mean_msgs']:.1f}")
    print(f"  Mean tokens used:       {l['mean_tokens']:.0f}")
    print()

    adv_msgs = g["mean_total_msgs"] - l["mean_msgs"]
    adv_toks = g["mean_tokens"] - l["mean_tokens"]
    print("  ── Graph vs. Linear Advantage ──────────────────────────")
    print(f"  Additional messages:    {adv_msgs:+.1f} per query")
    print(f"  Additional tokens:      {adv_toks:+.0f} per query")
    print()

    print("  ── Quality Metrics ─────────────────────────────────────")
    print(f"  Reframing rate:         {report['reframing_rate']:.1%}")
    print(f"  Mean composite score:   {report['quality_mean_composite']:.3f}")
    print()

    print("  ── Success Criteria ────────────────────────────────────")
    rf_icon = "✅" if sc["reframing_pass"] else "❌"
    dn_icon = "✅" if sc["density_pass"] else "❌"
    print(f"  {rf_icon} Reframing rate:  {sc['reframing_rate_actual']:.1%}  (target: {sc['reframing_rate_target']})")
    print(f"  {dn_icon} Context density: {sc['density_actual']:.1%}  (target: {sc['density_target']})")
    print("=" * 62)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2: Shadow mode evaluation")
    parser.add_argument("--since", metavar="YYYY-MM-DD")
    parser.add_argument("--until", metavar="YYYY-MM-DD")
    parser.add_argument("--budget", type=int, default=4000, help="Token budget for assembly")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="MessageStore DB path")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--report", action="store_true", help="Print report after run")
    args = parser.parse_args()

    report = run_shadow(
        db_path=args.db,
        start_date=args.since,
        end_date=args.until,
        token_budget=args.budget,
        verbose=args.verbose,
    )

    if report:
        print_report(report)
        print(f"\nShadow log:    {SHADOW_LOG}")
        print(f"Shadow report: {SHADOW_REPORT}")


if __name__ == "__main__":
    main()
