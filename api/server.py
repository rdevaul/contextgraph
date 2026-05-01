import sys
import re
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from store import MessageStore, Message
from features import extract_features
from tagger import _assign_tags_full
from ensemble import build_ensemble
from assembler import ContextAssembler, _estimate_tokens
from quality import QualityAgent
from tag_registry import get_registry
from sticky import StickyPinManager
from reframing import detect_reference
from utils.text import strip_envelope
from summarizer import summarize_message
from logger import _is_automated_turn
import pickle
import os
import json
import yaml

# Cache for parsed tags.yaml — hot-reloaded on each request by checking mtime
_tags_yaml_cache: tuple = None  # (mtime, data)
from typing import Optional
from collections import Counter
from datetime import datetime, timedelta


def _parse_since(since: Optional[str]) -> Optional[float]:
    """Convert a window string (e.g. '1d', '7d', '24h') to a Unix timestamp.
    Returns None for 'all' or unparsable values (meaning: no time filter)."""
    if not since or since == 'all':
        return None
    m = re.match(r'^(\d+)([dhm])$', since)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    if unit == 'd':
        delta = timedelta(days=value)
    elif unit == 'h':
        delta = timedelta(hours=value)
    else:
        delta = timedelta(minutes=value)
    return (datetime.utcnow() - delta).timestamp()


def _is_retrieval_turn(entry: dict) -> bool:
    """Return True if this turn represents a genuine retrieval attempt."""
    user_text = entry.get("userText", "") or entry.get("user_text", "")
    if not user_text:
        return False
    # System callbacks (subagent completion events)
    if user_text.startswith("System:"):
        return False
    # Subagent context turns (isolated sessions, no prior history)
    if "[Subagent Context]" in user_text[:200]:
        return False
    # Cron watcher turns (lightweight monitoring, not semantic queries)
    if user_text.startswith("[cron:3d4fde45"):  # local-watcher cron ID
        return False
    return True

app = FastAPI()

class TagRequest(BaseModel):
    user_text: str
    assistant_text: str

class IngestRequest(BaseModel):
    id: str = Field(None, nullable=True)
    session_id: str
    user_text: str
    assistant_text: str
    timestamp: float
    user_id: str = Field(None, nullable=True)
    external_id: str = Field(None, nullable=True)  # OpenClaw AgentMessage.id or other external system ID
    channel_label: str = Field(None, nullable=True)  # Channel label for per-agent memory isolation

class ToolState(BaseModel):
    last_turn_had_tools: bool
    pending_chain_ids: list[str] = Field(default_factory=list)

class AssembleRequest(BaseModel):
    user_text: str
    tags: list[str] | None = None
    token_budget: int = 4000
    tool_state: ToolState | None = None
    session_id: str | None = None
    # Per-bus thread 20260501213940-5b002851 / approval 20260501220916-a4feb6f0:
    # cross-pane content bleed required threading channel_label, user_tags,
    # and a scope mode through the assemble path. Defaults preserve legacy
    # global behavior so older plugin builds keep working until they upgrade.
    channel_label: str | None = None
    user_tags: list[str] | None = None
    # Default 'user' per bus approval 20260501220916-a4feb6f0:
    # cross-pane DM-style continuity for non-multigraph callers, while
    # multigraph dashboard panes opt into scope='session' explicitly.
    scope: str = "user"

class PinRequest(BaseModel):
    message_ids: list[str]
    reason: str
    ttl_turns: int = 20

class UnpinRequest(BaseModel):
    pin_id: str

class CompareResponse(BaseModel):
    graph_assembly: dict
    linear_window: dict
    inferred_tags: list = []

store = MessageStore()
quality_agent = QualityAgent()
# Build ensemble with FixedTagger + baseline in "fixed" mode (production mode)
ensemble = build_ensemble(mode="fixed", quality_agent=quality_agent)
pin_manager = StickyPinManager()

# Summarization configuration
SUMMARIZE_THRESHOLD = int(os.getenv("SUMMARIZE_THRESHOLD", "2000"))

def _background_summarize(message_id: str) -> None:
    """Background task to generate and store a summary for a message."""
    try:
        msg = store.get_by_id(message_id)
        if msg is None:
            return
        summary = summarize_message(msg)
        store.set_summary(message_id, summary)
    except Exception as e:
        # Log but don't propagate — this is fire-and-forget
        import logging
        logging.error(f"Background summarization failed for {message_id}: {e}")

# Register baseline tagger for fallback/ensemble voting
baseline_tagger = lambda features, user_text, assistant_text: _assign_tags_full(features, user_text, assistant_text)
ensemble.register('baseline', baseline_tagger, 1.0)

@app.on_event("startup")
async def startup_event():
    store.get_all_tags()  # Initialize the store
    # System tags loaded from tags.yaml via TagRegistry — single source of truth.

@app.post("/tag", response_model=dict)
def tag(request: TagRequest):
    try:
        features = extract_features(request.user_text, request.assistant_text)
        result = ensemble.assign(features, request.user_text, request.assistant_text)
        return {"tags": result.tags, "confidence": result.confidence, "per_tagger": result.per_tagger}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions?", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.IGNORECASE),
    re.compile(r"new\s+instructions?:", re.IGNORECASE),
    re.compile(r"system\s*prompt\s*:", re.IGNORECASE),
    re.compile(r"<\s*/?system\s*>", re.IGNORECASE),
    re.compile(r"\[INST\]|\[/INST\]", re.IGNORECASE),
    re.compile(r"###\s*instruction", re.IGNORECASE),
    re.compile(r"from\s+now\s+on", re.IGNORECASE),
    re.compile(r"\[SYSTEM\]\s*:", re.IGNORECASE),
    re.compile(r"<!--.*?-->", re.DOTALL),
]

# Strip zero-width characters that bypass pattern matching
_ZERO_WIDTH = re.compile(r'[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad]')

def _sanitize_for_storage(text: str) -> str:
    """Strip prompt injection patterns before storing in the graph."""
    if not text:
        return text
    # Normalize: strip zero-width chars that can bypass pattern matching
    normalized = _ZERO_WIDTH.sub('', text)
    result = normalized
    for pattern in _INJECTION_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result

@app.post("/ingest", response_model=dict)
def ingest(request: IngestRequest):
    try:
        message_id = request.id if request.id else f"api-{time.time()}"
        # Strip OpenClaw channel metadata envelopes before indexing.
        # Envelope text (message_id, sender_id, timestamps) is noise for
        # tag inference and retrieval — stripping prevents tag pollution.
        clean_user = strip_envelope(request.user_text)
        # HIGH-01 fix: sanitize injection patterns before storage
        clean_user = _sanitize_for_storage(clean_user)

        # Auto-detect automated turns (cron, heartbeat, local-watcher)
        is_automated = _is_automated_turn(request.user_text)

        # Skip tagging for automated messages - they shouldn't be tagged
        if is_automated:
            tags = []
        else:
            features = extract_features(clean_user, request.assistant_text)
            tags = ensemble.assign(features, clean_user, request.assistant_text).tags
            # Record hit for each tag assigned
            registry = get_registry()
            for tag in tags:
                registry.record_hit(tag)
        token_count = len(clean_user.split()) + len(request.assistant_text.split())
        message = Message(
            id=message_id,
            session_id=request.session_id,
            user_text=clean_user,
            assistant_text=request.assistant_text,
            timestamp=request.timestamp,
            user_id=request.user_id or "default",
            tags=tags,
            token_count=token_count,
            external_id=request.external_id,
            is_automated=is_automated,
            channel_label=request.channel_label,
        )
        store.add_message(message)

        # Kick off background summarization if message exceeds threshold
        if token_count > SUMMARIZE_THRESHOLD:
            thread = threading.Thread(
                target=_background_summarize,
                args=(message_id,),
                daemon=True
            )
            thread.start()

        return {"ingested": True, "tags": tags}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/assemble", response_model=dict)
def assemble(request: AssembleRequest):
    try:
        # Tick the pin manager to expire stale pins
        expired = pin_manager.tick()

        # Handle tool_state auto-pinning
        if request.tool_state and request.tool_state.last_turn_had_tools:
            # Auto-create or extend tool chain pin
            chain_ids = request.tool_state.pending_chain_ids
            if chain_ids:
                # Calculate token cost for the chain
                total_tokens = 0
                for msg_id in chain_ids:
                    # Try external_id first (OpenClaw IDs), then internal ID
                    msg = store.get_by_external_id(msg_id)
                    if msg is None:
                        msg = store.get_by_id(msg_id)
                    if msg:
                        total_tokens += _estimate_tokens(msg)

                pin_manager.update_or_create_tool_chain_pin(
                    message_ids=chain_ids,
                    reason="Active tool chain in progress",
                    total_tokens=total_tokens,
                    ttl_turns=10
                )
            else:
                # Server-side fallback: plugin lost state (e.g. gateway restart).
                # pending_chain_ids is empty but last_turn_had_tools=True, so we
                # know a tool chain was active. Pin the most recent messages from
                # the store as a best-effort recovery.
                # Use session-scoped get_recent if session_id is provided for better isolation
                if request.session_id:
                    recent = store.get_recent_by_session(5, request.session_id)
                else:
                    recent = store.get_recent(5)
                if recent:
                    fallback_ids = [msg.id for msg in recent]
                    total_tokens = sum(_estimate_tokens(msg) for msg in recent)
                    pin_manager.update_or_create_tool_chain_pin(
                        message_ids=fallback_ids,
                        reason="Active tool chain (server-side fallback: chain IDs lost on restart)",
                        total_tokens=total_tokens,
                        ttl_turns=10
                    )

        # Handle reference detection
        if detect_reference(request.user_text):
            # Find the most recent substantial work thread
            # (defined as: a sequence of ≥3 messages with tool-like activity)
            # For now, we'll pin the most recent 5 messages as a heuristic
            recent = store.get_recent(5)
            if len(recent) >= 3:
                msg_ids = [msg.id for msg in recent]
                total_tokens = sum(_estimate_tokens(msg) for msg in recent)
                pin_manager.add_pin(
                    message_ids=msg_ids,
                    pin_type="reference",
                    reason=f"Reference detected: '{request.user_text[:50]}...'",
                    ttl_turns=5,
                    total_tokens=total_tokens
                )

        # Strip envelope from query text for clean tag inference and similarity matching
        clean_query = strip_envelope(request.user_text)
        features = extract_features(clean_query, "")  # Empty assistant_text for incoming message
        if not request.tags:
            request.tags = ensemble.assign(features, clean_query, "").tags

        # Get pinned message IDs
        pinned_ids = pin_manager.get_pinned_message_ids()

        # Validate scope; coerce unknown values to 'global' for forward compat.
        requested_scope = request.scope if request.scope in ("session", "user", "global") else "global"

        assembler = ContextAssembler(store, token_budget=request.token_budget)
        result = assembler.assemble(
            clean_query,
            request.tags,
            pinned_message_ids=pinned_ids,
            channel_label=request.channel_label,
            user_tags=request.user_tags,
            session_id=request.session_id,
            scope=requested_scope,
        )

        return {
            "messages": [{"id": msg.id, "user_text": msg.user_text, "assistant_text": msg.assistant_text, "tags": msg.tags, "timestamp": msg.timestamp} for msg in result.messages],
            "total_tokens": result.total_tokens,
            "sticky_count": result.sticky_count,
            "recency_count": result.recency_count,
            "topic_count": result.topic_count,
            "tags_used": result.tags_used,
            "expired_pins": expired
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health", response_model=dict)
def health():
    try:
        messages_in_store = store.count()  # Actual count via SELECT COUNT(*)
        tags = store.get_all_tags()
        return {"status": "ok", "messages_in_store": messages_in_store, "tags": tags, "engine": "contextgraph"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/quality", response_model=dict)
def quality():
    """
    Retrieval quality metrics — the health check that actually tells you if
    the graph is working, not just if the service is up.

    Returns:
      - zero_return_rate: fraction of recent turns returning 0 graph messages
      - avg_topic_messages: average topic-layer message count across recent turns
      - tag_entropy: how evenly tags are distributed (low = over-generic tags)
      - top_tags: most frequent tags with corpus frequency
      - alert: true if quality is likely degraded
    """
    try:
        import math

        COMPARISON_LOG = Path.home() / ".tag-context" / "comparison-log.jsonl"
        RECENT_WINDOW = 50  # evaluate last N turns

        entries = []
        if COMPARISON_LOG.exists():
            with open(COMPARISON_LOG) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

        recent = entries[-RECENT_WINDOW:] if len(entries) > RECENT_WINDOW else entries

        # Filter to genuine retrieval turns only for quality metrics
        retrieval_entries = [e for e in recent if _is_retrieval_turn(e)]
        total = len(retrieval_entries)

        zero_return_turns = 0
        topic_msg_counts = []
        for e in retrieval_entries:
            # Support both flat (new) and nested (legacy) log schemas
            if "graphTokens" in e:
                tokens = e.get("graphTokens", 0)
                topic = e.get("graphTopic", 0)
            else:
                g = e.get("graph_assembly", {})
                tokens = g.get("tokens", 0)
                topic = g.get("topic", 0)
            if tokens == 0:
                zero_return_turns += 1
            topic_msg_counts.append(topic)

        zero_return_rate = zero_return_turns / total if total > 0 else 0.0
        avg_topic = sum(topic_msg_counts) / len(topic_msg_counts) if topic_msg_counts else 0.0

        # Tag entropy — measure how evenly distributed tags are
        tag_counts = store.tag_counts()
        total_corpus = len(store.get_recent(10000)) or 1
        total_tag_assignments = sum(tag_counts.values()) or 1
        entropy = 0.0
        for cnt in tag_counts.values():
            p = cnt / total_tag_assignments
            if p > 0:
                entropy -= p * math.log2(p)

        # Top tags with corpus frequency %
        top_tags = [
            {"tag": tag, "count": cnt, "corpus_pct": round(cnt / total_corpus * 100, 1)}
            for tag, cnt in list(tag_counts.items())[:10]
        ]

        # Alert thresholds
        alert = zero_return_rate > 0.25 or entropy < 2.0

        return {
            "turns_evaluated": len(recent),           # total turns in window
            "retrieval_turns_evaluated": total,        # after filtering non-retrieval
            "zero_return_turns": zero_return_turns,
            "zero_return_rate": round(zero_return_rate, 3),
            "avg_topic_messages": round(avg_topic, 2),
            "tag_entropy": round(entropy, 3),
            "corpus_size": total_corpus,
            "top_tags": top_tags,
            "alert": alert,
            "alert_reasons": [
                *(["zero_return_rate > 25%"] if zero_return_rate > 0.25 else []),
                *(["tag_entropy < 2.0 (over-generic tags)"] if entropy < 2.0 else []),
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/metrics", response_model=dict)
def metrics():
    try:
        # Build quality stats dict from all tagger IDs
        quality_stats = {}
        for tagger_id in quality_agent.all_tagger_ids():
            stats = quality_agent.stats(tagger_id)
            if stats:
                quality_stats[tagger_id] = {
                    "fitness": quality_agent.fitness(tagger_id),
                    "mean_density": stats.mean_density(),
                    "mean_reframing": stats.mean_reframing()
                }

        # Build tagger fitness from ensemble
        tagger_fitness = {}
        for entry in ensemble._taggers:
            tagger_fitness[entry.tagger_id] = entry.weight

        return {"quality_stats": quality_stats, "tagger_fitness": tagger_fitness}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/tags", response_model=dict)
def get_tags(since: Optional[str] = Query(None)):
    """Return system and user tags with metadata for /tags command in plugins.

    Since parameter: "1d" (last 24h), "7d" (last 7 days), None (all time).
    """
    try:
        registry = get_registry()

        # Calculate cutoff timestamp if a window is specified
        cutoff = _parse_since(since)

        # System tags: all tags from the registry with state, hits, and salience.
        corpus_size = store.count()
        if cutoff:
            tag_counts = store.tag_counts(since=cutoff)
            salience_scores = store.tag_salience(since=cutoff)
        else:
            tag_counts = store.tag_counts()
            salience_scores = store.tag_salience()

        system_tags = []
        for tag_name, cfg in registry._configs.items():
            hits = tag_counts.get(tag_name, 0)
            corpus_pct = (hits / corpus_size) if corpus_size > 0 else 0.0
            system_tags.append({
                "name": tag_name,
                "state": cfg.state,
                "hits": hits,
                "corpus_pct": round(corpus_pct, 4),
                "salience": round(salience_scores.get(tag_name, 0.0), 4),
            })

        # User tags: aggregate from per-user registries, deduplicating by
        # (name, channel). Only include tags with user-specific data
        # (hits > 0 or candidate/archived state) to avoid echoing
        # every system tag for every user.
        from tag_registry import get_user_registry, USER_REGISTRY_DIR
        user_tags_map = {}  # (name, channel) -> tag dict
        if USER_REGISTRY_DIR.exists():
            for reg_file in sorted(USER_REGISTRY_DIR.glob("*.json")):
                channel_label = reg_file.stem
                try:
                    user_reg = get_user_registry(channel_label)
                    if user_reg:
                        for tag_name, cfg in user_reg._configs.items():
                            rt = user_reg._runtime.get(tag_name)
                            tag_hits = rt.hits if rt else 0
                            key = (tag_name, channel_label)
                            if key in user_tags_map:
                                user_tags_map[key]["hits"] += tag_hits
                            else:
                                salience = salience_scores.get(tag_name, 0.0)
                                user_tags_map[key] = {
                                    "name": tag_name,
                                    "state": cfg.state,
                                    "hits": tag_hits,
                                    "channel": channel_label,
                                    "salience": round(salience, 4),
                                }
                except Exception:
                    pass

        # Filter: show all user tags except archived
        user_tags = [
            t for t in user_tags_map.values()
            if t["state"] != "archived"
        ]
        # Sort by hits descending
        user_tags.sort(key=lambda t: (-t["hits"], t["name"]))

        return {
            "system_tags": system_tags,
            "user_tags": user_tags,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/compare", response_model=CompareResponse)
def compare(request: TagRequest):
    try:
        features = extract_features(request.user_text, request.assistant_text)
        inferred_tags = ensemble.assign(features, request.user_text, request.assistant_text).tags

        # Graph Assembly — READ-ONLY: Get pinned IDs but do NOT tick the pin manager
        pinned_ids = pin_manager.get_pinned_message_ids()

        assembler = ContextAssembler(store, token_budget=4000)
        graph_assembly_result = assembler.assemble(request.user_text, inferred_tags, pinned_message_ids=pinned_ids)
        graph_assembly = {
            "messages": [{"id": msg.id, "user_text": msg.user_text, "assistant_text": msg.assistant_text, "tags": msg.tags, "timestamp": msg.timestamp} for msg in graph_assembly_result.messages],
            "total_tokens": graph_assembly_result.total_tokens,
            "sticky_count": graph_assembly_result.sticky_count,
            "recency_count": graph_assembly_result.recency_count,
            "topic_count": graph_assembly_result.topic_count,
            "tags_used": graph_assembly_result.tags_used
        }

        # Simulated Linear Window — pack to 4000 token budget, newest-first
        linear_window_messages = []
        linear_tokens = 0
        budget = 4000

        for msg in store.get_recent(100):  # Fetch enough to fill budget
            msg_tokens = len(msg.user_text.split()) + len(msg.assistant_text.split())
            if linear_tokens + msg_tokens > budget:
                break
            linear_window_messages.append(msg)
            linear_tokens += msg_tokens

        # Reverse to oldest-first for consistency with graph assembly
        linear_window_messages.reverse()

        linear_window = {
            "messages": [{"id": msg.id, "user_text": msg.user_text, "assistant_text": msg.assistant_text, "tags": msg.tags, "timestamp": msg.timestamp} for msg in linear_window_messages],
            "total_tokens": linear_tokens,
            "recency_count": len(linear_window_messages),
            "topic_count": len(set(tag for msg in linear_window_messages for tag in msg.tags)),
            "tags_used": list(set(tag for msg in linear_window_messages for tag in msg.tags))
        }

        return CompareResponse(graph_assembly=graph_assembly, linear_window=linear_window, inferred_tags=graph_assembly.get("tags_used", []))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Comparison Log Endpoints ───────────────────────────────────────────────────

@app.get("/comparison-log", response_model=list)
def get_comparison_log(limit: Optional[int] = Query(None, description="Maximum number of entries to return")):
    """Return comparison log entries from ~/.tag-context/comparison-log.jsonl (most recent first)."""
    try:
        log_path = Path.home() / ".tag-context" / "comparison-log.jsonl"
        if not log_path.exists():
            return []

        entries = []
        with open(log_path, 'r') as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))

        # Most recent first
        entries.reverse()

        if limit is not None and limit > 0:
            entries = entries[:limit]

        return entries
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/comparison-stats", response_model=dict)
def get_comparison_stats(since: Optional[str] = Query(None)):
    """Compute aggregate statistics from the comparison log.

    Since parameter: "1d" (last 24h), "7d" (last 7 days), None (all time).
    """
    try:
        log_path = Path.home() / ".tag-context" / "comparison-log.jsonl"
        if not log_path.exists():
            return {
                "total_turns": 0,
                "avg_graph_tokens": 0,
                "avg_linear_tokens": 0,
                "avg_graph_messages": 0,
                "avg_linear_messages": 0,
                "avg_tags_per_query": 0,
                "efficiency_ratio": 0,
                "token_savings_pct": 0,
                "time_series": [],
                "tag_frequency": {}
            }

        entries = []
        with open(log_path, 'r') as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))

        # Filter by time window if specified
        if since:
            from datetime import datetime, timedelta, timezone as tz
            now = datetime.now(tz.utc)
            if since == "1d":
                cutoff = now - timedelta(days=1)
            elif since == "7d":
                cutoff = now - timedelta(days=7)
            elif since == "30d":
                cutoff = now - timedelta(days=30)
            else:
                cutoff = None

            if cutoff:
                def _parse_ts(e):
                    """Handle both ISO strings and Unix timestamps."""
                    ts_val = e.get("timestamp", 0)
                    if isinstance(ts_val, (int, float)):
                        return datetime.fromtimestamp(ts_val, tz=tz.utc)
                    # ISO string: "2026-03-14T04:42:48.148Z"
                    if isinstance(ts_val, str):
                        ts_val = ts_val.replace("Z", "+00:00")
                        try:
                            return datetime.fromisoformat(ts_val)
                        except ValueError:
                            return datetime.min.replace(tzinfo=tz.utc)
                    return datetime.min.replace(tzinfo=tz.utc)

                entries = [e for e in entries if _parse_ts(e) >= cutoff]

        if not entries:
            return {
                "total_turns": 0,
                "avg_graph_tokens": 0,
                "avg_linear_tokens": 0,
                "avg_graph_messages": 0,
                "avg_linear_messages": 0,
                "avg_tags_per_query": 0,
                "efficiency_ratio": 0,
                "token_savings_pct": 0,
                "time_series": [],
                "tag_frequency": {}
            }

        def _graph_field(e, flat_key, nested_obj, nested_key, default=0):
            """Read from flat (new) or nested (legacy) log schema."""
            if flat_key in e:
                return e.get(flat_key, default)
            return e.get(nested_obj, {}).get(nested_key, default)

        def _linear_field(e, flat_key, nested_obj, nested_key, default=0):
            if flat_key in e:
                return e.get(flat_key, default)
            return e.get(nested_obj, {}).get(nested_key, default)

        total_turns = len(entries)
        total_graph_tokens = sum(_graph_field(e, "graphTokens", "graph_assembly", "tokens") for e in entries)
        total_linear_tokens = sum(_linear_field(e, "linearTokens", "linear_would_have", "tokens") for e in entries)
        total_graph_messages = sum(_graph_field(e, "graphMsgCount", "graph_assembly", "messages") for e in entries)
        total_linear_messages = sum(_linear_field(e, "linearMsgCount", "linear_would_have", "messages") for e in entries)

        avg_graph_tokens = total_graph_tokens / total_turns
        avg_linear_tokens = total_linear_tokens / total_turns
        avg_graph_messages = total_graph_messages / total_turns
        avg_linear_messages = total_linear_messages / total_turns

        # Calculate tag usage
        all_tags = []
        for entry in entries:
            tags = entry.get("graphTags") or entry.get("graph_assembly", {}).get("tags", [])
            all_tags.extend(tags)

        avg_tags_per_query = len(all_tags) / total_turns if total_turns > 0 else 0

        # Efficiency ratio: (linear - graph) / linear = token savings
        if total_linear_tokens > 0:
            efficiency_ratio = (total_linear_tokens - total_graph_tokens) / total_linear_tokens
            token_savings_pct = efficiency_ratio * 100
        else:
            efficiency_ratio = 0
            token_savings_pct = 0

        # Time series data (most recent 50 entries, chronological order)
        time_series = []
        recent_entries = entries[-50:] if len(entries) > 50 else entries
        for i, entry in enumerate(recent_entries):
            time_series.append({
                "index": i,
                "timestamp": entry.get("timestamp", ""),
                "graph_tokens": _graph_field(entry, "graphTokens", "graph_assembly", "tokens"),
                "linear_tokens": _linear_field(entry, "linearTokens", "linear_would_have", "tokens"),
                "graph_messages": _graph_field(entry, "graphMsgCount", "graph_assembly", "messages"),
                "linear_messages": _linear_field(entry, "linearMsgCount", "linear_would_have", "messages"),
            })

        # Tag frequency
        tag_counter = Counter(all_tags)
        tag_frequency = dict(tag_counter.most_common(20))  # Top 20 tags

        return {
            "total_turns": total_turns,
            "avg_graph_tokens": round(avg_graph_tokens, 2),
            "avg_linear_tokens": round(avg_linear_tokens, 2),
            "avg_graph_messages": round(avg_graph_messages, 2),
            "avg_linear_messages": round(avg_linear_messages, 2),
            "avg_tags_per_query": round(avg_tags_per_query, 2),
            "efficiency_ratio": round(efficiency_ratio, 4),
            "token_savings_pct": round(token_savings_pct, 2),
            "time_series": time_series,
            "tag_frequency": tag_frequency
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Dashboard Endpoint ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard():
    """Serve the context graph dashboard."""
    try:
        dashboard_path = Path(__file__).parent / "dashboard.html"
        with open(dashboard_path, 'r') as f:
            return f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error loading dashboard: {str(e)}")

# ── Tag Registry Endpoints ─────────────────────────────────────────────────────

@app.get("/registry", response_model=dict)
def get_tag_registry():
    """Return current tag registry state (core/candidate/archived tags with metadata)."""
    try:
        registry = get_registry()
        return registry.get_all_tags()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/registry/promote", response_model=dict)
def force_promote_tag(tag_name: str):
    """Force-promote a candidate tag to core."""
    try:
        registry = get_registry()
        success = registry.force_promote(tag_name)
        if success:
            return {"success": True, "message": f"Tag '{tag_name}' promoted to core"}
        else:
            raise HTTPException(status_code=400, detail=f"Cannot promote tag '{tag_name}' (not a candidate or doesn't exist)")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/registry/demote", response_model=dict)
def force_demote_tag(tag_name: str):
    """Force-archive a core tag."""
    try:
        registry = get_registry()
        success = registry.force_demote(tag_name)
        if success:
            return {"success": True, "message": f"Tag '{tag_name}' archived"}
        else:
            raise HTTPException(status_code=400, detail=f"Cannot demote tag '{tag_name}' (not a core tag or doesn't exist)")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Registry tick removed — no promotion/demotion in explicit-only system.
@app.post("/registry/tick", response_model=dict)
def registry_tick():
    """DEPRECATED: No longer performs promotion/demotion.
    System tags are explicit-only (see docs/TAG_SYSTEM_DESIGN.md)."""
    return {
        "promoted": [],
        "demoted": [],
        "message": "Tick no longer runs — explicit-only tag system. See docs/TAG_SYSTEM_DESIGN.md.",
    }

# ── Sticky Pin Endpoints ──────────────────────────────────────────────────

@app.post("/pin", response_model=dict)
def create_pin(request: PinRequest):
    """Create an explicit pin for specific messages."""
    try:
        # Calculate total tokens for the pinned messages
        total_tokens = 0
        for msg_id in request.message_ids:
            # Try external_id first (OpenClaw IDs), then internal ID
            msg = store.get_by_external_id(msg_id)
            if msg is None:
                msg = store.get_by_id(msg_id)
            if msg:
                total_tokens += _estimate_tokens(msg)

        pin_id = pin_manager.add_pin(
            message_ids=request.message_ids,
            pin_type="explicit",
            reason=request.reason,
            ttl_turns=request.ttl_turns,
            total_tokens=total_tokens
        )

        return {
            "success": True,
            "pin_id": pin_id,
            "message": f"Created pin with {len(request.message_ids)} messages"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/unpin", response_model=dict)
def remove_pin(request: UnpinRequest):
    """Remove a pin by ID.

    What this does: Deletes a pin from the in-memory StickyPinManager.
    The underlying message remains in the database; only the "keep in
    context" flag is removed.

    What it does NOT do: Does NOT delete messages, does NOT change tags.
    """
    try:
        success = pin_manager.remove_pin(request.pin_id)
        if success:
            return {
                "success": True,
                "message": f"Pin {request.pin_id} removed"
            }
        else:
            raise HTTPException(status_code=404, detail=f"Pin {request.pin_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/pins", response_model=dict)
def get_pins():
    """Get all active pins with their status."""
    try:
        active_pins = pin_manager.get_active_pins()
        pins_data = []

        for pin in active_pins:
            pins_data.append({
                "pin_id": pin.pin_id,
                "pin_type": pin.pin_type,
                "message_ids": pin.message_ids,
                "reason": pin.reason,
                "ttl_turns": pin.ttl_turns,
                "turns_elapsed": pin.turns_elapsed,
                "turns_remaining": pin.ttl_turns - pin.turns_elapsed,
                "total_tokens": pin.total_tokens,
                "created_at": pin.created_at
            })

        return {
            "active_pins": pins_data,
            "total_pins": len(pins_data),
            "total_tokens": sum(p.total_tokens for p in active_pins)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Admin Endpoints ───────────────────────────────────────────────────────────

class MergeLabelsRequest(BaseModel):
    source_labels: list[str]
    target_label: str
    dry_run: bool = True


@app.post("/admin/merge-channel-labels", response_model=dict)
def admin_merge_channel_labels(req: MergeLabelsRequest):
    """Merge source channel_label values into a single target label.

    PURPOSE: Before channel_labels.yaml, each sender ID was stored as its own
    label (e.g. "994902066" for Telegram, "510637988242522133" for Discord).
    This endpoint retroactively consolidates them into canonical usernames.

    DRY RUN (default): Set dry_run=true to preview changes. No data modified.

    LIVE: Set dry_run=false. A timestamped DB backup is created first.

    WARNING: Always dry-run first. This permanently modifies channel_label
    values in the message store.
    """
    if not req.source_labels:
        raise HTTPException(status_code=400, detail="source_labels must be non-empty")
    if not req.target_label:
        raise HTTPException(status_code=400, detail="target_label must be non-empty")

    if req.dry_run:
        stats = store.get_channel_label_stats()
        affected = {}
        for src in req.source_labels:
            display_src = src or "(null)"
            affected[display_src] = stats.get(display_src, {"count": 0, "sessions": 0})
        total = sum(a["count"] for a in affected.values())
        return {
            "dry_run": True,
            "action": "merge_channel_labels",
            "source_labels": req.source_labels,
            "target_label": req.target_label,
            "affected": affected,
            "total_rows_affected": total,
            "note": "Set dry_run=false in the request body to execute.",
        }

    backup_path = _create_backup_db()
    if not backup_path:
        raise HTTPException(status_code=500, detail="Failed to create backup before merge")

    result = store.merge_channel_labels(req.source_labels, req.target_label)
    return {
        "dry_run": False,
        "action": "merge_channel_labels",
        "source_labels": req.source_labels,
        "target_label": req.target_label,
        "rows_updated": result["rows_updated"],
        "affected_id_count": len(result["affected_ids"]),
        "backup_path": str(backup_path),
        "note": "Run POST /admin/retag to rebuild tags on merged messages.",
    }


@app.post("/admin/merge-all-channel-labels", response_model=dict)
def admin_merge_all_channel_labels(req: MergeLabelsRequest):
    """Merge ALL non-null, non-target labels into target.

    PURPOSE: Nuclear option — consolidates every fragmented label (numeric
    IDs, unknowns, etc.) into one canonical label (e.g. "rich").

    DRY RUN: Shows every label that would be merged with counts.
    LIVE: Creates backup, then merges.

    Skips target_label and NULL labels. NULL labels (pre-channel_label
    column messages) are NOT touched.
    """
    stats = store.get_channel_label_stats()
    to_merge = {
        label: data
        for label, data in stats.items()
        if label != "(null)" and label != req.target_label
    }
    total = sum(d["count"] for d in to_merge.values())

    if req.dry_run or not to_merge:
        return {
            "dry_run": True,
            "target_label": req.target_label,
            "labels_to_merge": to_merge,
            "total_rows_affected": total,
            "note": "Set dry_run=false to execute.",
        }

    backup_path = _create_backup_db()
    if not backup_path:
        raise HTTPException(status_code=500, detail="Failed to create backup")

    source_labels = list(to_merge.keys())
    result = store.merge_channel_labels(source_labels, req.target_label)
    return {
        "dry_run": False,
        "labels_merged": source_labels,
        "rows_updated": result["rows_updated"],
        "backup_path": str(backup_path),
        "note": "Run POST /admin/retag to rebuild tags.",
    }


class RetagRequest(BaseModel):
    message_ids: Optional[list[str]] = None
    limit: int = 100


@app.post("/admin/retag", response_model=dict)
def admin_retag(req: RetagRequest):
    """Re-run tagging on existing messages.

    PURPOSE: After merging labels or updating tags.yaml, rebuild tags on
    existing messages so retrieval quality is restored.

    If message_ids provided: only those messages are retagged.
    If not provided: the N most recent non-automated messages are retagged.

    WARNING: CPU-intensive at scale. Use during low-traffic periods.
    Each message requires a full ensemble tagger evaluation.
    """
    if req.message_ids:
        messages = []
        for mid in req.message_ids:
            msg = store.get_by_id(mid)
            if msg:
                messages.append(msg)
        if not messages:
            raise HTTPException(status_code=400, detail="No valid message_ids found")
    else:
        messages = store.get_non_automated(limit=req.limit)

    retagged = 0
    for msg in messages:
        clean_user = strip_envelope(msg.user_text)
        features = extract_features(clean_user, msg.assistant_text)
        new_tags = ensemble.assign(features, clean_user, msg.assistant_text).tags
        if set(new_tags) != set(msg.tags):
            with store._lock:
                conn = store._conn()
                conn.execute("DELETE FROM tags WHERE message_id = ?", (msg.id,))
                for tag in new_tags:
                    conn.execute(
                        "INSERT OR IGNORE INTO tags (message_id, tag) VALUES (?, ?)",
                        (msg.id, tag),
                    )
                conn.commit()
            retagged += 1

    return {
        "retagged": retagged,
        "total_processed": len(messages),
        "unchanged": len(messages) - retagged,
    }


# ── Per-Channel Endpoints ─────────────────────────────────────────────────────

@app.get("/channels", response_model=dict)
def list_channels():
    """List all channel labels with message counts and tag counts.

    PURPOSE: Drive the channel selector dropdown in the dashboard.
    Non-destructive, safe to call anytime.

    Returns: {label: {message_count, session_count, tag_count}} for every
    distinct channel_label.
    """
    stats = store.get_channel_label_stats()
    result = {}
    for label, data in stats.items():
        display = label or "(null)"
        # Count tags for messages in this channel
        tag_count = store.channel_tag_count(label)
        result[display] = {
            "message_count": data["count"],
            "session_count": data["sessions"],
            "tag_count": tag_count,
        }
    # Sort by message count descending
    return {"channels": dict(sorted(result.items(), key=lambda x: -x[1]["message_count"]))}


@app.get("/quality/channel/{channel_label}", response_model=dict)
def channel_quality(channel_label: str):
    """Compute quality metrics scoped to a specific channel label."""
    try:
        # Fetch messages for this channel
        messages = store.get_recent(50, channel_label=channel_label)
        if not messages:
            return {
                "channel": channel_label,
                "turns_evaluated": 0,
                "zero_return_rate": None,
                "avg_topic_messages": None,
                "tag_entropy": None,
            }

        # Compute tag entropy for this channel's recent messages
        all_tags: dict[str, int] = {}
        for msg in messages:
            for tag in msg.tags:
                all_tags[tag] = all_tags.get(tag, 0) + 1

        total_tag_hits = sum(all_tags.values())
        if total_tag_hits == 0:
            entropy = 0.0
        else:
            import math
            entropy = 0.0
            for count in all_tags.values():
                p = count / total_tag_hits
                if p > 0:
                    entropy -= p * math.log2(p)

        return {
            "channel": channel_label,
            "turns_evaluated": len(messages),
            "unique_tags": len(all_tags),
            "avg_tags_per_message": total_tag_hits / len(messages),
            "tag_entropy": round(entropy, 3),
            "top_tags": sorted(all_tags.items(), key=lambda x: -x[1])[:15],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tags/channel/{channel_label}", response_model=dict)
def channel_tags(channel_label: str):
    """Get tag distribution for a specific channel label."""
    try:
        tag_counts = store.channel_tag_counts(channel_label)
        total = sum(tag_counts.values()) if tag_counts else 0
        result = []
        for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
            result.append({"name": tag, "count": count, "pct": round(count / total * 100, 1) if total > 0 else 0})
        return {"channel": channel_label, "total_messages_tagged": total, "tags": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/compare/channel/{channel_label}", response_model=CompareResponse)
def compare_channel(channel_label: str, request: TagRequest):
    """Run a comparison (graph vs linear) scoped to a specific channel."""
    try:
        features = extract_features(request.user_text, request.assistant_text)
        inferred_tags = ensemble.assign(features, request.user_text, request.assistant_text).tags

        pinned_ids = pin_manager.get_pinned_message_ids()

        assembler = ContextAssembler(store, token_budget=4000)
        graph_assembly_result = assembler.assemble(
            request.user_text, inferred_tags,
            pinned_message_ids=pinned_ids,
            channel_label=channel_label
        )
        graph_assembly = {
            "messages": [{"id": msg.id, "user_text": msg.user_text, "assistant_text": msg.assistant_text, "tags": msg.tags, "timestamp": msg.timestamp} for msg in graph_assembly_result.messages],
            "total_tokens": graph_assembly_result.total_tokens,
            "sticky_count": graph_assembly_result.sticky_count,
            "recency_count": graph_assembly_result.recency_count,
            "topic_count": graph_assembly_result.topic_count,
            "tags_used": graph_assembly_result.tags_used,
        }

        # Linear window: just recent messages from this channel
        linear_window_messages = []
        linear_tokens = 0
        budget = 4000
        for msg in store.get_recent(100, channel_label=channel_label):
            msg_tokens = len(msg.user_text.split()) + len(msg.assistant_text.split())
            if linear_tokens + msg_tokens > budget:
                break
            linear_window_messages.append(msg)
            linear_tokens += msg_tokens
        linear_window_messages.reverse()

        linear_window = {
            "messages": [{"id": msg.id, "user_text": msg.user_text, "assistant_text": msg.assistant_text, "tags": msg.tags, "timestamp": msg.timestamp} for msg in linear_window_messages],
            "total_tokens": linear_tokens,
            "recency_count": len(linear_window_messages),
            "topic_count": len(set(tag for msg in linear_window_messages for tag in msg.tags)),
            "tags_used": list(set(tag for msg in linear_window_messages for tag in msg.tags)),
        }

        return CompareResponse(graph_assembly=graph_assembly, linear_window=linear_window, inferred_tags=graph_assembly.get("tags_used", []))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/channel-labels", response_model=dict)
def admin_channel_labels():
    """List all channel labels with counts and session counts.

    PURPOSE: Verify label distribution after deploying channel_labels.yaml
    or after a merge. Non-destructive, safe to call anytime.

    Returns: {label: {count, sessions}} for every distinct channel_label.
    """
    stats = store.get_channel_label_stats()
    return {
        "channel_labels": stats,
        "total_labels": len(stats),
        "total_messages": sum(d["count"] for d in stats.values()),
    }


def _load_tags_yaml() -> list[dict]:
    """Load tags.yaml and return the list of tag definitions.
    Hot-reloads by checking file mtime on each call."""
    global _tags_yaml_cache
    yaml_path = Path(__file__).parent.parent / "tags.yaml"
    mtime = yaml_path.stat().st_mtime if yaml_path.exists() else 0
    if _tags_yaml_cache and _tags_yaml_cache[0] == mtime:
        return _tags_yaml_cache[1]
    if not yaml_path.exists():
        return []
    data = yaml.safe_load(yaml_path.read_text())
    tags = data.get("tags", []) if isinstance(data, dict) else []
    _tags_yaml_cache = (mtime, tags)
    return tags


@app.get("/tag-rules", response_model=dict)
def get_tag_rules(tag: str = Query(default=None)):
    """Return matching rules for tags from tags.yaml.
    
    If 'tag' query param is provided, return rules for that specific tag.
    Otherwise return all tag rules. Useful for debugging tag matching.
    """
    tags = _load_tags_yaml()
    if tag:
        # Exact match first, then prefix match
        matches = [t for t in tags if t.get("name") == tag]
        if not matches:
            matches = [t for t in tags if t.get("name", "").startswith(tag.lower())]
        if not matches:
            # Fuzzy: name contains the query
            matches = [t for t in tags if tag.lower() in t.get("name", "")]
        if matches:
            return {"tag": matches[0]}
        else:
            raise HTTPException(status_code=404, detail=f"Tag '{tag}' not found in tags.yaml")
    return {"tags": tags, "count": len(tags)}


def _create_backup_db() -> Optional[Path]:
    """Create a timestamped backup of the store database before destructive ops.

    Returns the backup path, or None on failure.
    """
    try:
        from datetime import datetime
        import shutil

        db_path = Path(store._db_path)
        if not db_path.exists():
            return None

        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"store_{ts}.db"

        shutil.copy2(str(db_path), str(backup_path))

        if backup_path.exists() and backup_path.stat().st_size == db_path.stat().st_size:
            return backup_path
        return None
    except Exception:
        import logging
        logging.exception("Failed to create database backup")
        return None

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8302)
