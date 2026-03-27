"""
assembler.py — Context assembly policy for the tag-context system.

Builds a context window from a combination of recent messages (recency layer)
and tag-retrieved messages (topic layer), packed to a token budget.

Tag IDF filtering: tags appearing in >30% of the corpus are treated as stop
words for topic retrieval (they carry no discriminating signal). This threshold
is configurable via TOPIC_TAG_MAX_CORPUS_FREQ.
"""

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

    def __init__(self, store: MessageStore, token_budget: int = 4000) -> None:
        self.store = store
        self.token_budget = token_budget

    def assemble(self, incoming_text: str,
                 inferred_tags: List[str],
                 pinned_message_ids: Optional[List[str]] = None,
                 channel_label: Optional[str] = None,
                 user_tags: Optional[List[str]] = None) -> AssemblyResult:
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
            If set, user-scoped tags (in `user_tags`) will only retrieve messages
            from this channel. System tags always retrieve from all channels.
        user_tags : Optional[List[str]]
            Tags that are user-scoped (should be filtered by channel_label).
            If None, all tags are treated as system tags (no channel filter).
        """
        # ── Sticky layer ───────────────────────────────────────────────────
        sticky_msgs: List[Message] = []
        sticky_tokens = 0

        if pinned_message_ids:
            sticky_budget = int(self.token_budget * 0.3)
            # Try to fetch by external_id first (for OpenClaw IDs), fall back to internal ID
            for msg_id in pinned_message_ids:
                msg = self.store.get_by_external_id(msg_id)
                if msg is None:
                    # Fallback to internal ID lookup for backwards compatibility
                    msg = self.store.get_by_id(msg_id)
                if msg is None:
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
        for msg in self.store.get_recent(10):
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
        # Use tag_counts sum as corpus size proxy — avoids fetching all rows.
        # tag_counts() returns {tag: count} where count = messages with that tag.
        # Total unique messages ≈ max tag count (most frequent tag upper-bounds corpus).
        tag_counts = self.store.tag_counts()
        total_messages = max(tag_counts.values()) if tag_counts else 1
        if total_messages == 0:
            total_messages = 1  # avoid div-by-zero
        useful_tags = [
            t for t in inferred_tags
            if tag_counts.get(t, 0) / total_messages <= TOPIC_TAG_MAX_CORPUS_FREQ
        ]
        # Fall back to all tags if every tag is high-frequency (small corpus)
        if not useful_tags and inferred_tags:
            # Sort by ascending frequency and take the bottom half
            useful_tags = sorted(inferred_tags, key=lambda t: tag_counts.get(t, 0))
            useful_tags = useful_tags[: max(1, len(useful_tags) // 2)]

        # Build a set of user-scoped tag names for channel filtering
        user_tag_set = set(user_tags) if user_tags else set()

        for tag in useful_tags:
            # Apply channel_label filter only for user-scoped tags
            tag_channel = channel_label if (channel_label and tag in user_tag_set) else None
            for msg in self.store.get_by_tag(tag, limit=20, channel_label=tag_channel):
                if msg.id not in seen_ids:
                    topic_candidates.append(msg)
                    seen_ids.add(msg.id)

        # newest-first within topic candidates, then pack to budget
        topic_candidates.sort(key=lambda m: m.timestamp, reverse=True)

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
