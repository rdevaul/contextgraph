import sys
import re
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from store import MessageStore, Message
from features import extract_features
from tagger import assign_tags
from ensemble import EnsembleTagger
from assembler import ContextAssembler, _estimate_tokens
from quality import QualityAgent
from gp_tagger import GeneticTagger
from tag_registry import get_registry
from sticky import StickyPinManager
from reframing import detect_reference
from utils.text import strip_envelope
import pickle
import os
import json
from typing import Optional
from collections import Counter

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

class ToolState(BaseModel):
    last_turn_had_tools: bool
    pending_chain_ids: list[str] = Field(default_factory=list)

class AssembleRequest(BaseModel):
    user_text: str
    tags: list[str] | None = None
    token_budget: int = 4000
    tool_state: ToolState | None = None
    session_id: str | None = None

class PinRequest(BaseModel):
    message_ids: list[str]
    reason: str
    ttl_turns: int = 20

class UnpinRequest(BaseModel):
    pin_id: str

class CompareResponse(BaseModel):
    graph_assembly: dict
    linear_window: dict

store = MessageStore()
quality_agent = QualityAgent()
ensemble = EnsembleTagger(quality_agent=quality_agent)
pin_manager = StickyPinManager()

gp_tagger_path = Path(__file__).parent.parent / 'data' / 'gp-tagger.pkl'
if gp_tagger_path.exists():
    with open(gp_tagger_path, 'rb') as f:
        gp_tagger = pickle.load(f)
        ensemble.register(gp_tagger.tagger_id, gp_tagger.assign, 1.0)

baseline_tagger = lambda features, user_text, assistant_text: assign_tags(features, user_text, assistant_text)
ensemble.register('baseline', baseline_tagger, 1.0)

@app.on_event("startup")
async def startup_event():
    store.get_all_tags()  # Initialize the store

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
        features = extract_features(clean_user, request.assistant_text)
        tags = ensemble.assign(features, clean_user, request.assistant_text).tags
        message = Message(
            id=message_id,
            session_id=request.session_id,
            user_text=clean_user,
            assistant_text=request.assistant_text,
            timestamp=request.timestamp,
            user_id=request.user_id or "default",
            tags=tags,
            token_count=len(clean_user.split()) + len(request.assistant_text.split()),
            external_id=request.external_id
        )
        store.add_message(message)
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

        assembler = ContextAssembler(store, token_budget=request.token_budget)
        result = assembler.assemble(clean_query, request.tags, pinned_message_ids=pinned_ids)

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
        messages_in_store = len(store.get_recent(1000))  # Approximate count
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
        total = len(recent)

        zero_return_turns = 0
        topic_msg_counts = []
        for e in recent:
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
            "turns_evaluated": total,
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

        return CompareResponse(graph_assembly=graph_assembly, linear_window=linear_window)
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
def get_comparison_stats():
    """Compute aggregate statistics from the comparison log."""
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

@app.post("/registry/tick", response_model=dict)
def registry_tick():
    """Run promotion and demotion cycle on tag registry."""
    try:
        registry = get_registry()
        promoted = registry.promote_candidates()
        demoted = registry.demote_stale()
        return {
            "promoted": promoted,
            "demoted": demoted,
            "message": f"Promoted {len(promoted)} candidates, archived {len(demoted)} stale tags"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    """Remove a pin by ID."""
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8350)
