"""
assembler.py — Context assembly policy for the tag-context system.

Builds a context window from a combination of recent messages (recency layer)
and tag-retrieved messages (topic layer), packed to a token budget.

Tag IDF filtering: tags appearing in >30% of the corpus are treated as stop
words for topic retrieval (they carry no discriminating signal). This threshold
is configurable via TOPIC_TAG_MAX_CORPUS_FREQ.
"""

import os
from dataclasses import dataclass
from typing import List, Optional

from store import Message, MessageStore
from summarizer import summarize_message

# Tags appearing in more than this fraction of the corpus are skipped in
# topic retrieval. At >30% they're effectively stop words (e.g. "code",
# "openclaw" in a corpus of AI assistant interactions).
TOPIC_TAG_MAX_CORPUS_FREQ = 0.30

# A single message will not be included if it exceeds this fraction of the
# total token budget — prevents one giant turn from consuming the entire window.
# The "always include first" safety valve is also capped at this size.
MAX_SINGLE_MSG_BUDGET_FRACTION = 0.35

# Fraction of token budget reserved for pinned/sticky messages.
# Set STICKY_BUDGET_FRACTION env var to override (default 0.3 = 30%).
DEFAULT_STICKY_BUDGET_FRACTION = float(os.environ.get("STICKY_BUDGET_FRACTION", "0.3"))


def _estimate_tokens(msg: Message) -> int:
    """Estimate tokens for a message (use stored count or word-count proxy)."""
    if msg.token_count > 0:
        return msg.token_count
    words = len((msg.user_text + " " + msg.assistant_text).split())
    return max(1, int(words * 1.3))


@dataclass
class AssemblyResult:
    """Result of a context assembly operation."""
    messages: List[Message]     # oldest-first, ready to use as context
    total_tokens: int
    sticky_count: int           # how many came from the sticky layer
    recency_count: int          # how many came from the recency layer
    topic_count: int            # how many came from the topic layer
    tags_used: List[str]        # tags that contributed to topic layer


class ContextAssembler:
    """
    Assembles context for an incoming message from three layers:

    1. Sticky layer   (up to 30% of budget) — pinned messages (tool chains, explicit pins)
    2. Recency layer  (20-25% of budget)    — most recent messages regardless of tag
    3. Topic layer    (50-75% of budget)    — messages retrieved by inferred tags,
                                              deduplicated against sticky + recency

    When sticky layer is empty, budget reallocates to recency (25%) and topic (75%).

    Final result is sorted oldest-first for natural reading order.

    User-scoped assembly: if channel_label is provided, the topic layer filters
    messages to that user's channel for user-scoped tags, while system tags
    retrieve from all channels. Pass user_tags to specify which tags are user-scoped.
    """

    def __init__(self, store: MessageStore, token_budget: int = 4000,
                 sticky_budget_fraction: float = DEFAULT_STICKY_BUDGET_FRACTION) -> None:
        self.store = store
        self.token_budget = token_budget
        self.sticky_budget_fraction = sticky_budget_fraction

    def assemble(self, incoming_text: str,
                 inferred_tags: List[str],
                 pinned_message_ids: Optional[List[str]] = None,
                 channel_label: Optional[str] = None,
                 user_tags: Optional[List[str]] = None,
                 session_id: Optional[str] = None,
                 scope: str = "user") -> AssemblyResult:
        """
        Build a context window for `incoming_text` given `inferred_tags`.

        Parameters
        ----------
        incoming_text : str
            The user's new message (used only for future tag inference hooks).
        inferred_tags : List[str]
            Tags inferred for the incoming message by the tagger.
        pinned_message_ids : Optional[List[str]]
            Message IDs that should be pinned in the sticky layer.
            If None, sticky layer is skipped.
        channel_label : Optional[str]
            If set AND scope=='user', the recency + topic layers filter
            messages to this channel_label. Provides cross-user isolation
            (e.g. rich's panes don't pull garrett's content). This is the
            Part A fix: previously the comment in this file claimed it did
            this, but no actual filtering was wired — leakage was complete.
        user_tags : Optional[List[str]]
            Forward-compat hint (currently unused; channel_label filtering
            in scope='user' is unconditional). Reserved for future per-tag
            scope policy.
        session_id : Optional[str]
            Reserved for Part B (`scope='session'` per-pane isolation).
            In Part A this is accepted for API stability but not yet used
            for filtering — 'session' falls through to 'user' behavior.
        scope : str
            One of:
              - 'user'   : filter recency + topic by channel_label (cross-user
                           isolation; default).
              - 'global' : no filtering — the legacy behavior. Reserved as an
                           explicit escape hatch for cross-pane research views.
              - 'session': reserved for Part B; falls through to 'user' here.
        """
        if scope not in ("session", "user", "global"):
            raise ValueError(f"invalid scope {scope!r}; expected session|user|global")
        # ── Sticky layer ───────────────────────────────────────────────────
        sticky_msgs: List[Message] = []
        sticky_tokens = 0

        if pinned_message_ids:
            sticky_budget = int(self.token_budget * self.sticky_budget_fraction)
            # Try to fetch by external_id first (for OpenClaw IDs), fall back to internal ID
            for msg_id in pinned_message_ids:
                msg = self.store.get_by_external_id(msg_id)
                if msg is None:
                    # Fallback to internal ID lookup for backwards compatibility
                    msg = self.store.get_by_id(msg_id)
                if msg is None:
                    continue
                # Per-user scope: drop pins from other channel_labels so that
                # rich's pin can't leak into garrett's assemble call.
                if scope == "user" and channel_label is not None and msg.channel_label is not None \
                        and msg.channel_label != channel_label:
                    continue
                cost = _estimate_tokens(msg)
                if sticky_tokens + cost > sticky_budget:
                    break
                sticky_msgs.append(msg)
                sticky_tokens += cost

        # ── Budget allocation ──────────────────────────────────────────────
        remaining_budget = self.token_budget - sticky_tokens

        if sticky_msgs:
            # When sticky is active: recency 20%, topic 50%+remainder
            recency_budget = int(remaining_budget * 0.25)
        else:
            # When no sticky: recency 25%, topic 75% (original behavior)
            recency_budget = int(remaining_budget * 0.25)

        topic_budget = remaining_budget - recency_budget

        # ── Recency layer ──────────────────────────────────────────────────
        seen_ids = {m.id for m in sticky_msgs}
        recency_msgs: List[Message] = []
        recency_tokens = 0

        # Cap: no single message may exceed this fraction of the total budget.
        single_msg_cap = int(self.token_budget * MAX_SINGLE_MSG_BUDGET_FRACTION)

        first_recency = True
        # ── Recency source selection by scope (Part A) ────────────────────────
        # 'session' is reserved for Part B and currently routes through 'user'
        # behavior — the bus approval sequence is Part A first (cross-user),
        # Part B second (cross-pane).
        if (scope == "user" or scope == "session") and channel_label:
            recency_source = self.store.get_recent_by_channel(10, channel_label)
        else:
            # scope == 'global', or scope='user' with no channel_label hint
            recency_source = self.store.get_recent(10)

        for msg in recency_source:
            if msg.id in seen_ids:
                continue
            cost = _estimate_tokens(msg)

            # ── Oversized message handling ─────────────────────────────────
            # Check for summary substitution BEFORE the global cap, so that
            # a 5000-token message with a 100-token summary doesn't falsely
            # trigger the break.
            effective_msg = msg
            if cost > single_msg_cap:
                summary_text = msg.summary
                if not summary_text:
                    try:
                        summary_text = summarize_message(msg)
                        self.store.set_summary(msg.id, summary_text)
                    except Exception:
                        summary_text = None

                if summary_text:
                    user_preview = msg.user_text[:200] + "..." if len(msg.user_text) > 200 else msg.user_text
                    effective_msg = Message(
                        id=msg.id,
                        session_id=msg.session_id,
                        user_id=msg.user_id,
                        timestamp=msg.timestamp,
                        user_text=user_preview,
                        assistant_text=summary_text,
                        tags=msg.tags,
                        token_count=len(summary_text.split()),
                        external_id=msg.external_id,
                        summary=None
                    )
                    cost = _estimate_tokens(effective_msg)
                else:
                    # No summary available — skip this oversized message entirely
                    continue

            # Hard global cap: never exceed the total token budget.
            if sticky_tokens + recency_tokens + cost > self.token_budget:
                break
            if not first_recency and recency_tokens + cost > recency_budget:
                break
            recency_msgs.append(effective_msg)
            recency_tokens += cost
            seen_ids.add(msg.id)
            first_recency = False

        # ── Topic layer ────────────────────────────────────────────────────
        topic_candidates: List[Message] = []

        # IDF filtering: skip tags that are too common to be discriminating.
        # Tags in >30% of corpus are stop words — they retrieve nearly everything,
        # blowing the token budget on low-relevance messages.
        #
        # IDF filtering: skip tags that appear too frequently to be discriminating.
        # Use actual message count as corpus denominator.
        tag_counts = self.store.tag_counts()
        total_messages = self.store.count() if self.store.count() > 0 else 1
        useful_tags = [
            t for t in inferred_tags
            if tag_counts.get(t, 0) / total_messages <= TOPIC_TAG_MAX_CORPUS_FREQ
        ]
        # Fall back to all tags if every tag is high-frequency (small corpus)
        if not useful_tags and inferred_tags:
            # Sort by ascending frequency and take the bottom half
            useful_tags = sorted(inferred_tags, key=lambda t: tag_counts.get(t, 0))
            useful_tags = useful_tags[: max(1, len(useful_tags) // 2)]

        # Build a set of user-scoped tag names for channel filtering.
        # Forward-compat hint; not currently used to gate filtering, since
        # channel_label filtering is unconditional within scope='user'.
        user_tag_set = set(user_tags) if user_tags else set()

        # Track how many tags each candidate matches — used for scoring below.
        tag_hit_count: dict = {}

        # Pick the topic-layer query based on scope (Part A wires channel_label;
        # Part B will add a session_id branch on top of this).
        if (scope == "user" or scope == "session") and channel_label:
            tag_filter_kwargs = {"channel_label": channel_label}
        else:
            tag_filter_kwargs = {}

        for tag in useful_tags:
            if tag_filter_kwargs:
                fetched = self.store.get_by_tag_scoped(tag, limit=50, **tag_filter_kwargs)
            else:
                # scope='global' (or missing scope key) — preserve legacy behavior.
                fetched = self.store.get_by_tag(tag, limit=50)
            for msg in fetched:
                if msg.id not in seen_ids:
                    topic_candidates.append(msg)
                    seen_ids.add(msg.id)
                tag_hit_count[msg.id] = tag_hit_count.get(msg.id, 0) + 1

        # Score candidates: prefer messages that match multiple tags (higher relevance)
        # and are reasonably recent. Pure recency sort was causing the staircase pattern —
        # it always returned the same newest-N messages regardless of semantic fit.
        import time as _time
        now_ts = _time.time()

        def _score(m: Message) -> float:
            # Recency component: exponential decay over ~30 days
            age_days = max(0, (now_ts - m.timestamp) / 86400)
            recency_score = 2 ** (-age_days / 30)
            # Tag hit component: messages matching more of the query tags rank higher
            tag_score = tag_hit_count.get(m.id, 1)
            return tag_score * 2 + recency_score  # tag relevance weighted 2x over recency

        topic_candidates.sort(key=_score, reverse=True)

        topic_msgs: List[Message] = []
        topic_tokens = 0
        first_topic = True

        for msg in topic_candidates:
            cost = _estimate_tokens(msg)

            # ── Oversized message handling (same pattern as recency layer) ─
            effective_msg = msg
            if cost > single_msg_cap:
                summary_text = msg.summary
                if not summary_text:
                    try:
                        summary_text = summarize_message(msg)
                        self.store.set_summary(msg.id, summary_text)
                    except Exception:
                        summary_text = None

                if summary_text:
                    user_preview = msg.user_text[:200] + "..." if len(msg.user_text) > 200 else msg.user_text
                    effective_msg = Message(
                        id=msg.id,
                        session_id=msg.session_id,
                        user_id=msg.user_id,
                        timestamp=msg.timestamp,
                        user_text=user_preview,
                        assistant_text=summary_text,
                        tags=msg.tags,
                        token_count=len(summary_text.split()),
                        external_id=msg.external_id,
                        summary=None
                    )
                    cost = _estimate_tokens(effective_msg)
                else:
                    continue  # No summary — skip oversized message

            # Hard global cap
            if sticky_tokens + recency_tokens + topic_tokens + cost > self.token_budget:
                break
            if not first_topic and topic_tokens + cost > topic_budget:
                break
            topic_msgs.append(effective_msg)
            topic_tokens += cost
            first_topic = False

        # ── Combine + sort oldest-first ────────────────────────────────────
        all_msgs = sticky_msgs + recency_msgs + topic_msgs
        all_msgs.sort(key=lambda m: m.timestamp)

        return AssemblyResult(
            messages=all_msgs,
            total_tokens=sticky_tokens + recency_tokens + topic_tokens,
            sticky_count=len(sticky_msgs),
            recency_count=len(recency_msgs),
            topic_count=len(topic_msgs),
            tags_used=useful_tags,
        )
