import sys
import re
import time
import threading
from datetime import datetime, timedelta
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
from tag_registry import get_registry, get_user_registry, USER_REGISTRY_DIR
from sticky import StickyPinManager
from reframing import detect_reference
from utils.text import strip_envelope
from summarizer import summarize_message
from logger import _is_automated_turn
import config
import pickle
import os
import json
from typing import Optional
from collections import Counter

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
    user_text: str = Field(..., max_length=100_000)
    assistant_text: str = Field(..., max_length=100_000)

class IngestRequest(BaseModel):
    id: str = Field(None, nullable=True, max_length=256)
    session_id: str = Field(..., max_length=256)
    user_text: str = Field(..., max_length=100_000)
    assistant_text: str = Field(..., max_length=100_000)
    timestamp: float = Field(..., ge=0)
    user_id: str = Field(None, nullable=True, max_length=256)
    external_id: str = Field(None, nullable=True, max_length=256)  # OpenClaw AgentMessage.id or other external system ID
    channel_label: str = Field(None, nullable=True, max_length=256)  # Channel label for per-agent memory isolation

class ToolState(BaseModel):
    last_turn_had_tools: bool
    pending_chain_ids: list[str] = Field(default_factory=list, max_length=100)

class AssembleRequest(BaseModel):
    user_text: str = Field(..., max_length=100_000)
    tags: list[str] | None = Field(None, max_length=50)
    token_budget: int = Field(4000, ge=100, le=200_000)
    tool_state: ToolState | None = None
    session_id: str | None = Field(None, max_length=256)
    channel_label: str | None = Field(None, max_length=256)

class AddUserTagRequest(BaseModel):
    name: str = Field(..., max_length=100)
    description: str = Field("", max_length=500)
    keywords: list[str] | None = Field(None, max_length=100)
    confidence: float = Field(1.0, ge=0.0, le=1.0)

class PinRequest(BaseModel):
    message_ids: list[str] = Field(..., max_length=100)
    reason: str = Field(..., max_length=1000)
    ttl_turns: int = Field(20, ge=1, le=1000)

class UnpinRequest(BaseModel):
    pin_id: str

class CompareResponse(BaseModel):
    inferred_tags: list[str] = []
    graph_assembly: dict
    linear_window: dict

store = MessageStore()
quality_agent = QualityAgent()
ensemble = EnsembleTagger(quality_agent=quality_agent)
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

# ── GP tagger (optional, experimental) ───────────────────────────────────────
# The GP tagger is DISABLED by default. To enable it, set:
#   CONTEXTGRAPH_TAGGER_MODE=hybrid   (fixed + gp)
#   CONTEXTGRAPH_TAGGER_MODE=gp-only  (gp only, not recommended)
#
# ⚠️  VOTING LIMITATION: With the current weighted-vote ensemble
# (fixed=1.5, baseline=1.0, gp=1.0, total=3.5, threshold=0.4),
# the GP's normalised weight is 1.0/3.5 ≈ 0.286 — BELOW the threshold.
# This means the GP can NEVER promote a tag on its own; it can only
# reinforce tags that fixed or baseline already found. Any unique tags
# the GP fires are always pruned. Until the voting system is redesigned
# (e.g. threshold lowered to 0.2, or GP given higher weight), the GP
# provides no recall improvement over fixed+baseline alone.
if config.TAGGER_MODE in ("hybrid", "gp-only"):
    gp_tagger_path = Path(__file__).parent.parent / 'data' / 'gp-tagger.pkl'
    if gp_tagger_path.exists():
        try:
            with open(gp_tagger_path, 'rb') as f:
                gp_tagger = pickle.load(f)
                ensemble.register(gp_tagger.tagger_id, gp_tagger.assign, 1.0)
            print(f"[contextgraph] GP tagger loaded (experimental mode={config.TAGGER_MODE})")
        except Exception as e:
            print(f"[contextgraph] WARNING: GP tagger failed to load, continuing without it: {e}")
    else:
        print(f"[contextgraph] GP tagger mode={config.TAGGER_MODE} but no pkl found at {gp_tagger_path}, skipping")
else:
    print(f"[contextgraph] Tagger mode={config.TAGGER_MODE} — GP tagger disabled (default)")

from fixed_tagger import FixedTagger, USER_TAGS_DIR
import re as _re

# Validate channel labels to prevent path traversal attacks.
# Labels must be alphanumeric with optional hyphens/underscores, 1-64 chars.
_VALID_LABEL_RE = _re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')

def _validate_label(label: str) -> str:
    """Validate a channel label to prevent path traversal. Raises HTTPException on invalid input."""
    if not _VALID_LABEL_RE.match(label):
        raise HTTPException(status_code=400, detail=f"Invalid channel label: '{label}'. Must be lowercase alphanumeric with hyphens/underscores, 1-64 chars.")
    return label

fixed_tagger_instance = FixedTagger()
ensemble.register('fixed', fixed_tagger_instance.assign, 1.5)  # Higher weight — authoritative for personal assistant tags

baseline_tagger = lambda features, user_text, assistant_text: assign_tags(features, user_text, assistant_text)
ensemble.register('baseline', baseline_tagger, 1.0)

@app.on_event("startup")
async def startup_event():
    store.get_all_tags()  # Initialize the store
    # One-time purge of junk candidates with < 2 hits
    registry = get_registry()
    purged = registry.purge_junk_candidates(min_hits=2)
    if purged > 0:
        print(f"[startup] Purged {purged} junk candidate tags with < 2 hits")
    _seed_registry_from_yaml()


def _seed_registry_from_yaml() -> None:
    """
    Auto-seed the tag registry with any enabled tags in tags.yaml that
    aren't already registered. This means adding a tag to tags.yaml is
    all that's needed — no manual registry surgery required.
    """
    import logging
    try:
        import yaml
    except ImportError:
        logging.warning("pyyaml not installed; skipping tags.yaml registry seed")
        return

    tags_yaml_path = Path(__file__).parent.parent / "tags.yaml"
    if not tags_yaml_path.exists():
        return

    try:
        with tags_yaml_path.open() as f:
            data = yaml.safe_load(f)
    except Exception as e:
        logging.warning(f"Failed to load tags.yaml for registry seeding: {e}")
        return

    registry = get_registry()
    active_tags = registry.get_active_tags()  # core + candidate
    archived_tags = set(registry.get_archived().keys())

    import time
    now = time.time()
    seeded = []

    for entry in data.get("tags", []):
        name = entry.get("name")
        if not name:
            continue
        if not entry.get("enabled", True):
            continue  # skip disabled tags
        if name in active_tags or name in archived_tags:
            continue  # already known to the registry

        # New tag in yaml — seed it as core so it's immediately active
        from tag_registry import TagMetadata
        registry._tags[name] = TagMetadata(
            name=name,
            state="core",
            first_seen=now,
            last_seen=now,
            hits=0,
            promoted_at=now,
        )
        seeded.append(name)

    if seeded:
        registry.save()
        print(f"[startup] Registry seeded {len(seeded)} new tag(s) from tags.yaml: {seeded}")

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

def _is_degenerate_text(text: str, threshold: float = 0.7) -> bool:
    """Detect garbage/repetitive text that would poison the graph.
    
    Returns True if the text is mostly repeated words (e.g. 'word word word...').
    Threshold is the ratio of most-common-word occurrences to total words.
    """
    if not text:
        return False
    words = text.lower().split()
    if len(words) < 10:
        return False
    from collections import Counter
    counts = Counter(words)
    most_common_count = counts.most_common(1)[0][1]
    return (most_common_count / len(words)) >= threshold


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

        # Reject degenerate/garbage messages (e.g. "word word word..." repeated)
        if _is_degenerate_text(clean_user):
            return {"status": "skipped", "reason": "degenerate text detected"}

        # Reject test/benchmark sessions — these come from pytest runs and
        # should never be stored in the live graph.
        if request.session_id and (
            request.session_id.startswith("test-") or
            request.session_id.startswith("test_") or
            request.session_id.startswith("pytest-") or
            request.session_id == "test"
        ):
            return {"status": "skipped", "reason": "test session rejected"}

        # Auto-detect automated turns (cron, heartbeat, local-watcher)
        is_automated = _is_automated_turn(request.user_text)

        features = extract_features(clean_user, request.assistant_text)
        tags = ensemble.assign(features, clean_user, request.assistant_text).tags
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

        # Build channel-aware tagger if channel_label provided
        channel_label = request.channel_label
        if channel_label:
            _validate_label(channel_label)
            from fixed_tagger import FixedTagger
            ch_tagger = FixedTagger.for_channel(channel_label)
        else:
            ch_tagger = fixed_tagger_instance

        features = extract_features(clean_query, "")  # Empty assistant_text for incoming message
        if not request.tags:
            request.tags = ch_tagger.assign(features, clean_query, "").tags

        # Determine user tags (tags that exist in user tag file)
        user_tag_names: list[str] = []
        if channel_label:
            user_tags_path = USER_TAGS_DIR / f"{channel_label}.yaml"
            if user_tags_path.exists():
                from fixed_tagger import FixedTagger as _FT, _parse_tag_specs
                import yaml as _yaml
                with user_tags_path.open() as _f:
                    _ud = _yaml.safe_load(_f)
                user_tag_names = [s.name for s in _parse_tag_specs(_ud or {})]

        # Get pinned message IDs
        pinned_ids = pin_manager.get_pinned_message_ids()

        assembler = ContextAssembler(store, token_budget=request.token_budget)
        result = assembler.assemble(
            clean_query, request.tags, pinned_message_ids=pinned_ids,
            channel_label=channel_label,
            user_tags=user_tag_names if user_tag_names else None,
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
        messages_in_store = store.count()  # Exact count via SELECT COUNT(*)
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
        # Use the same _estimate_tokens() function as the assembler for an apples-to-apples
        # comparison. Previously used raw word count (no 1.3x multiplier), which made the
        # linear window appear ~30% smaller than graph mode at equivalent budgets.
        linear_window_messages = []
        linear_tokens = 0
        budget = 4000

        for msg in store.get_recent(100):  # Fetch enough to fill budget
            msg_tokens = _estimate_tokens(msg)
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

        return CompareResponse(inferred_tags=inferred_tags, graph_assembly=graph_assembly, linear_window=linear_window)
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
def get_comparison_stats(since: Optional[str] = Query(None, description="ISO timestamp or hours suffix e.g. '24h', '7d' to filter entries")):
    """Compute aggregate statistics from the comparison log.
    
    Optional ?since= param filters entries to a time window:
      - ISO 8601 string: since=2026-03-23T00:00:00Z
      - Hours suffix:    since=24h
      - Days suffix:     since=7d
    """
    try:
        from datetime import timezone
        log_path = Path.home() / ".tag-context" / "comparison-log.jsonl"
        
        # Parse since param into a cutoff datetime
        cutoff_dt = None
        if since:
            import re as _re
            m = _re.match(r'^(\d+(?:\.\d+)?)(h|d)$', since.strip().lower())
            if m:
                amount, unit = float(m.group(1)), m.group(2)
                hours = amount if unit == 'h' else amount * 24
                cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
            else:
                try:
                    cutoff_dt = datetime.fromisoformat(since.replace('Z', '+00:00'))
                    if cutoff_dt.tzinfo is None:
                        cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass  # Ignore unparseable since values
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

        # Apply time window filter if cutoff_dt was parsed
        if cutoff_dt is not None:
            from datetime import timezone as _tz
            def _entry_ts(e):
                ts = e.get("timestamp", "")
                if not ts:
                    return None
                try:
                    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=_tz.utc)
                    return dt
                except ValueError:
                    return None
            entries = [e for e in entries if (ts := _entry_ts(e)) is not None and ts >= cutoff_dt]

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

# ── User Tag Endpoints ─────────────────────────────────────────────────────────

@app.get("/tags", response_model=dict)
def get_tags(channel_label: Optional[str] = Query(None, description="Channel label for user tags")):
    """
    Return combined system + user tags with registry metadata.

    If channel_label is provided, also returns user tags for that channel.
    """
    try:
        import yaml as _yaml

        registry = get_registry()
        tag_counts_map = store.tag_counts()

        def _build_tag_list(tags_dict, scope: str) -> list:
            result = []
            for name, meta in tags_dict.items():
                result.append({
                    "name": name,
                    "state": meta.state,
                    "hits": meta.hits,
                    "scope": scope,
                })
            return sorted(result, key=lambda x: x["hits"], reverse=True)

        all_sys = registry._tags
        system_tags = _build_tag_list(all_sys, "system")

        user_tags_list = []
        if channel_label:
            _validate_label(channel_label)
            user_reg = get_user_registry(channel_label)
            if user_reg:
                user_tags_list = _build_tag_list(user_reg._tags, "user")

        return {"system_tags": system_tags, "user_tags": user_tags_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tags/system", response_model=dict)
def get_system_tags():
    """Return system tags only."""
    try:
        registry = get_registry()
        result = []
        for name, meta in registry._tags.items():
            result.append({
                "name": name,
                "state": meta.state,
                "hits": meta.hits,
                "scope": "system",
            })
        return {"system_tags": sorted(result, key=lambda x: x["hits"], reverse=True)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tags/user/{label}", response_model=dict)
def get_user_tags(label: str):
    """Return user tags for a specific channel label."""
    try:
        _validate_label(label)
        user_reg = get_user_registry(label)
        result = []
        if user_reg:
            for name, meta in user_reg._tags.items():
                result.append({
                    "name": name,
                    "state": meta.state,
                    "hits": meta.hits,
                    "scope": "user",
                })
        return {"user_tags": sorted(result, key=lambda x: x["hits"], reverse=True), "channel_label": label}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tags/user/{label}/add", response_model=dict)
def add_user_tag(label: str, request: AddUserTagRequest):
    """
    Add a new user tag for a channel label.

    Writes to ~/.tag-context/tags.user/<label>.yaml (hot-reloaded by tagger).
    Also seeds the user registry with a candidate entry.
    """
    try:
        _validate_label(label)
        import yaml as _yaml

        user_tags_path = USER_TAGS_DIR / f"{label}.yaml"
        USER_TAGS_DIR.mkdir(parents=True, exist_ok=True)

        # Load existing or start fresh
        if user_tags_path.exists():
            with user_tags_path.open() as f:
                data = _yaml.safe_load(f) or {}
        else:
            data = {"version": 1, "tags": []}

        tags_list = data.get("tags", [])

        # Check for duplicate
        existing_names = [t["name"] for t in tags_list]
        if request.name in existing_names:
            raise HTTPException(status_code=409, detail=f"Tag '{request.name}' already exists for user '{label}'")

        # Infer keywords from name if not provided
        keywords = request.keywords
        if not keywords:
            # Generate keyword suggestions from tag name (replace hyphens/underscores with spaces)
            keywords = [request.name.replace("-", " ").replace("_", " ")]

        new_tag = {
            "name": request.name,
            "description": request.description,
            "keywords": keywords,
            "confidence": request.confidence,
        }
        tags_list.append(new_tag)
        data["tags"] = tags_list

        with user_tags_path.open("w") as f:
            _yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

        # Seed registry candidate
        import time as _time
        user_reg = get_user_registry(label)
        if user_reg and request.name not in user_reg._tags:
            from tag_registry import TagMetadata
            now = _time.time()
            user_reg._tags[request.name] = TagMetadata(
                name=request.name,
                state="core",  # new user tags are immediately core
                first_seen=now,
                last_seen=now,
                hits=0,
                promoted_at=now,
            )
            user_reg.save()

        return {
            "success": True,
            "tag": new_tag,
            "inferred_keywords": keywords if not request.keywords else None,
            "message": f"Tag '{request.name}' added for user '{label}'"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/tags/user/{label}/{tag_name}", response_model=dict)
def archive_user_tag(label: str, tag_name: str):
    """
    Archive a user tag (move to archived state; do not delete from YAML).

    Updates the user registry to mark the tag as archived.
    """
    try:
        _validate_label(label)
        user_reg = get_user_registry(label)
        if user_reg is None:
            raise HTTPException(status_code=404, detail=f"No registry for user '{label}'")

        if tag_name not in user_reg._tags:
            raise HTTPException(status_code=404, detail=f"Tag '{tag_name}' not found for user '{label}'")

        import time as _time
        tag = user_reg._tags[tag_name]
        tag.state = "archived"
        tag.archived_at = _time.time()
        user_reg.save()

        return {"success": True, "message": f"Tag '{tag_name}' archived for user '{label}'"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tags/user/{label}/retag", response_model=dict)
def retag_user_corpus(label: str):
    """
    Retag the user's corpus with current user tags.

    Only touches messages with matching channel_label. This re-runs tag
    inference on the user's messages and updates their tags in the store.
    """
    try:
        _validate_label(label)
        from fixed_tagger import FixedTagger
        from features import extract_features as _extract_features

        ch_tagger = FixedTagger.for_channel(label)
        messages = store.get_recent(10000, channel_label=label)

        updated = 0
        for msg in messages:
            features = _extract_features(msg.user_text, msg.assistant_text)
            new_tags = ch_tagger.assign(features, msg.user_text, msg.assistant_text).tags
            if set(new_tags) != set(msg.tags):
                # Clear old tags and re-add
                with store._lock:
                    conn = store._conn()
                    conn.execute("DELETE FROM tags WHERE message_id = ?", (msg.id,))
                    for tag in new_tags:
                        conn.execute(
                            "INSERT OR IGNORE INTO tags (message_id, tag) VALUES (?, ?)",
                            (msg.id, tag),
                        )
                    conn.commit()
                updated += 1

        return {
            "success": True,
            "channel_label": label,
            "messages_processed": len(messages),
            "messages_updated": updated,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Admin: Global Retag ────────────────────────────────────────────────────────

@app.post("/admin/retag", response_model=dict)
def retag_all(batch_size: int = Query(500, ge=50, le=5000)):
    """
    Re-tag the entire corpus with current system tagger rules.

    This re-runs the ensemble tagger on every stored message and updates tags.
    Use after changing tagger rules to backfill the new logic.
    Safe to run multiple times. Returns count of updated messages.
    """
    try:
        messages = store.get_recent(100_000)  # All messages
        updated = 0
        total = len(messages)
        for msg in messages:
            features = extract_features(msg.user_text, msg.assistant_text)
            new_tags = ensemble.assign(features, msg.user_text, msg.assistant_text).tags
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
                updated += 1
        return {
            "success": True,
            "messages_processed": total,
            "messages_updated": updated,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Backfill Endpoint ──────────────────────────────────────────────────────────

@app.post("/admin/backfill-channel-labels", response_model=dict)
def backfill_channel_labels():
    """
    One-time migration: set channel_label on existing messages based on session_id patterns.
    Safe to run multiple times (only updates NULL rows).
    """
    try:
        counts = store.backfill_channel_labels()
        return {"success": True, "updated": counts}
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
