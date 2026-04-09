"""
config.py — Environment variable configuration for contextgraph.

All paths and agent-specific settings are controlled via environment variables,
allowing multiple agents (GLaDOS, Jarvis, etc.) to run independent instances
on the same machine.
"""

import os
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


# Workspace root — the agent workspace directory
WORKSPACE = Path(_env(
    "CONTEXTGRAPH_WORKSPACE",
    str(Path.home() / ".openclaw" / "workspace")
))

# Agent name — used for launchd service naming and logging
AGENT_NAME = _env("CONTEXTGRAPH_AGENT_NAME", "glados")

# Database path
DB_PATH = Path(_env(
    "CONTEXTGRAPH_DB_PATH",
    str(Path.home() / ".tag-context" / "store.db")
))

# Tags config path
TAGS_CONFIG = Path(_env(
    "CONTEXTGRAPH_TAGS_CONFIG",
    str(Path(__file__).parent / "tags.yaml")
))

# Tagger mode controls which taggers are active in the ensemble.
#
# "fixed"   (default) — FixedTagger + baseline rule tagger only.
#            Recommended for production. Deterministic, auditable, fast.
#
# "hybrid"  (experimental) — adds the GP (genetic program) tagger.
#            ⚠️  WARNING: With the default vote threshold (0.4) and weights
#            (fixed=1.5, baseline=1.0, gp=1.0), the GP's normalised vote
#            (≈0.286) falls below the threshold. The GP can never promote
#            a tag that fixed+baseline haven't already found — it only adds
#            noise to the vote counts. Do not use unless you lower the
#            threshold or raise GP weight. Requires: pip install deap
#
# "gp-only" (legacy/experimental) — GP tagger only. Not recommended.
#            Requires: pip install deap
TAGGER_MODE = _env("CONTEXTGRAPH_TAGGER_MODE", "fixed")
