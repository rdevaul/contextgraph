#!/usr/bin/env python3
"""
verify_logging.py — Diagnostic tool for interaction logging health.

Checks:
1. Interaction log files (data/interactions/YYYY-MM-DD.jsonl)
2. Comparison log (~/.tag-context/comparison-log.jsonl)
3. API health (http://127.0.0.1:8302/health)
4. API stats (http://127.0.0.1:8302/comparison-stats)
5. Harvester state (data/harvester-state.json)
6. Coverage gaps (sessions not harvested)

Usage:
  python3 scripts/verify_logging.py [--date YYYY-MM-DD] [--verbose]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Set
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ── Paths ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent
INTERACTIONS_DIR = PROJECT_ROOT / "data" / "interactions"
COMPARISON_LOG = Path.home() / ".tag-context" / "comparison-log.jsonl"
HARVESTER_STATE = PROJECT_ROOT / "data" / "harvester-state.json"
SESSIONS_INDEX = Path.home() / ".openclaw/agents/main/sessions/sessions.json"

API_BASE = "http://127.0.0.1:8302"
API_HEALTH = f"{API_BASE}/health"
API_STATS = f"{API_BASE}/comparison-stats"

# ── Data Loading ─────────────────────────────────────────────────────────────

def count_jsonl_lines(path: Path) -> int:
    """Count non-empty lines in a JSONL file."""
    if not path.exists():
        return 0
    count = 0
    with path.open() as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def load_jsonl_records(path: Path) -> List[dict]:
    """Load all records from a JSONL file."""
    if not path.exists():
        return []
    records = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def get_interaction_sessions(records: List[dict]) -> Set[str]:
    """Extract unique session_ids from interaction log records."""
    return {r.get("session_id", "") for r in records if r.get("session_id")}


def get_openclaw_sessions() -> Dict[str, dict]:
    """Load OpenClaw sessions.json index."""
    if not SESSIONS_INDEX.exists():
        return {}
    with SESSIONS_INDEX.open() as f:
        return json.load(f)


def load_harvester_state() -> dict:
    """Load harvester state file."""
    if not HARVESTER_STATE.exists():
        return {}
    with HARVESTER_STATE.open() as f:
        return json.load(f)


def api_get(url: str, timeout: int = 5) -> dict:
    """Fetch JSON from API endpoint."""
    try:
        req = Request(url)
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return json.loads(data)
    except (URLError, HTTPError, json.JSONDecodeError) as e:
        return {"error": str(e)}


# ── Session Pattern Matching ─────────────────────────────────────────────────

INCLUDE_PATTERNS = [
    "agent:main:main",
    "agent:main:telegram:",
    "agent:main:voice",
    "agent:main:discord:",
    "agent:main:direct:",
    "agent:vera:",
]

EXCLUDE_PATTERNS = [
    ":cron:",
    ":hook:",
    ":group:",
]


def should_harvest(session_key: str) -> bool:
    """Check if session key should be harvested."""
    if any(pat in session_key for pat in EXCLUDE_PATTERNS):
        return False
    return any(pat in session_key for pat in INCLUDE_PATTERNS)


# ── Analysis ─────────────────────────────────────────────────────────────────

def analyze_comparison_log(records: List[dict]) -> dict:
    """Analyze comparison-log.jsonl records."""
    if not records:
        return {"turns": 0, "avg_tokens_saved": 0, "efficiency_pct": 0}

    total_saved = 0
    total_graph = 0
    total_linear = 0

    for rec in records:
        # Handle both formats: old (graph_tokens) and new (graph_assembly.tokens)
        if "graph_assembly" in rec:
            graph_tokens = rec.get("graph_assembly", {}).get("tokens", 0)
            linear_tokens = rec.get("linear_would_have", {}).get("tokens", 0)
        else:
            graph_tokens = rec.get("graph_tokens", 0)
            linear_tokens = rec.get("linear_tokens", 0)

        total_graph += graph_tokens
        total_linear += linear_tokens
        total_saved += (linear_tokens - graph_tokens)

    avg_saved = total_saved / len(records) if records else 0
    efficiency_pct = (total_saved / total_linear * 100) if total_linear > 0 else 0

    return {
        "turns": len(records),
        "avg_tokens_saved": int(avg_saved),
        "efficiency_pct": round(efficiency_pct, 1),
        "total_saved": total_saved,
        "total_graph": total_graph,
        "total_linear": total_linear,
    }


def find_coverage_gaps(openclaw_sessions: Dict[str, dict],
                      interaction_sessions: Set[str]) -> Dict[str, List[str]]:
    """Find sessions that should be harvested but aren't in interaction log."""
    harvestable = [k for k in openclaw_sessions.keys() if should_harvest(k)]
    missing = [k for k in harvestable if k not in interaction_sessions]

    discord_sessions = [k for k in openclaw_sessions.keys() if "discord" in k or "direct:" in k]
    discord_captured = [k for k in discord_sessions if k in interaction_sessions]

    return {
        "harvestable_total": harvestable,
        "missing": missing,
        "discord_sessions": discord_sessions,
        "discord_captured": discord_captured,
    }


# ── Main Report ──────────────────────────────────────────────────────────────

def generate_report(date: str, verbose: bool = False) -> str:
    """Generate logging health report."""
    today = datetime.strptime(date, "%Y-%m-%d")
    yesterday = today - timedelta(days=1)

    today_file = INTERACTIONS_DIR / f"{today.strftime('%Y-%m-%d')}.jsonl"
    yesterday_file = INTERACTIONS_DIR / f"{yesterday.strftime('%Y-%m-%d')}.jsonl"

    # Load data
    today_records = load_jsonl_records(today_file)
    yesterday_records = load_jsonl_records(yesterday_file)
    comparison_records = load_jsonl_records(COMPARISON_LOG)
    harvester_state = load_harvester_state()
    openclaw_sessions = get_openclaw_sessions()

    # API calls
    health = api_get(API_HEALTH)
    stats = api_get(API_STATS)

    # Analysis
    today_sessions = get_interaction_sessions(today_records)

    # Use API stats if available, fall back to local calculation
    if "error" not in stats and stats.get("total_turns", 0) > 0:
        comparison_stats = {
            "turns": stats.get("total_turns", 0),
            "avg_tokens_saved": int(stats.get("avg_linear_tokens", 0) - stats.get("avg_graph_tokens", 0)),
            "efficiency_pct": round(stats.get("token_savings_pct", 0), 1),
        }
    else:
        comparison_stats = analyze_comparison_log(comparison_records)

    gaps = find_coverage_gaps(openclaw_sessions, today_sessions)

    # Build report
    lines = []
    lines.append(f"=== Logging Health: {date} ===\n")

    # Interaction logs
    lines.append(f"Interaction log: {len(today_records)} records today, {len(yesterday_records)} yesterday")

    # Comparison log
    lines.append(f"Comparison log:  {comparison_stats['turns']} turns logged, "
                f"avg {comparison_stats['avg_tokens_saved']} tokens saved "
                f"({comparison_stats['efficiency_pct']}%)")

    # API health
    if "error" in health:
        lines.append(f"API health:      ERROR — {health['error']}")
    else:
        lines.append(f"API health:      OK — {health.get('messages_in_store', 0)} messages, "
                    f"{len(health.get('tags', []))} tags")

    # Harvester state
    if harvester_state:
        last_run = harvester_state.get("last_harvest_ts", 0)
        if last_run:
            last_run_str = datetime.fromtimestamp(last_run).strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_run_str = "never"
        sessions_in_state = len(harvester_state.get("sessions", {}))
        lines.append(f"Harvester state: last_run={last_run_str}, sessions_tracked={sessions_in_state}")
    else:
        lines.append("Harvester state: NOT FOUND")

    # Coverage gaps
    lines.append("\n=== Coverage Gaps ===")
    lines.append(f"Harvestable sessions: {len(gaps['harvestable_total'])}")
    lines.append(f"Missing from log:     {len(gaps['missing'])}")
    lines.append(f"Discord sessions:     {len(gaps['discord_sessions'])} total, "
                f"{len(gaps['discord_captured'])} captured")

    if gaps['discord_captured']:
        lines.append(f"Discord coverage:     YES")
    else:
        lines.append(f"Discord coverage:     NO")

    if verbose:
        lines.append("\n=== Verbose Details ===")
        if gaps['missing']:
            lines.append(f"\nMissing sessions ({len(gaps['missing'])}):")
            for s in gaps['missing'][:10]:
                lines.append(f"  - {s}")
            if len(gaps['missing']) > 10:
                lines.append(f"  ... and {len(gaps['missing']) - 10} more")

        if gaps['discord_sessions']:
            lines.append(f"\nDiscord sessions ({len(gaps['discord_sessions'])}):")
            for s in gaps['discord_sessions'][:10]:
                captured = "✓" if s in today_sessions else "✗"
                lines.append(f"  {captured} {s}")
            if len(gaps['discord_sessions']) > 10:
                lines.append(f"  ... and {len(gaps['discord_sessions']) - 10} more")

    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Verify interaction logging health")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                       default=datetime.now().strftime("%Y-%m-%d"),
                       help="Date to check (default: today)")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Show detailed session lists")
    args = parser.parse_args()

    try:
        # Validate date format
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(f"ERROR: Invalid date format: {args.date}", file=sys.stderr)
        print("Expected format: YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    report = generate_report(args.date, verbose=args.verbose)
    print(report)


if __name__ == "__main__":
    main()
