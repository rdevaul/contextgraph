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

# Tags appearing in more than this fraction of the corpus are skipped in
# topic retrieval. At >30% they're effectively stop words (e.g. "code",
# "openclaw" in a corpus of AI assistant interactions).
TOPIC_TAG_MAX_CORPUS_FREQ = 0.30


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
    """

    def __init__(self, store: MessageStore, token_budget: int = 4000) -> None:
        self.store = store
        self.token_budget = token_budget

    def assemble(self, incoming_text: str,
                 inferred_tags: List[str],
                 pinned_message_ids: Optional[List[str]] = None) -> AssemblyResult:
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

        for msg in self.store.get_recent(10):
            if msg.id in seen_ids:
                continue
            cost = _estimate_tokens(msg)
            if recency_tokens + cost > recency_budget:
                break
            recency_msgs.append(msg)
            recency_tokens += cost
            seen_ids.add(msg.id)

        # ── Topic layer ────────────────────────────────────────────────────
        topic_candidates: List[Message] = []

        # IDF filtering: skip tags that are too common to be discriminating.
        # Tags in >30% of corpus are stop words — they retrieve nearly everything,
        # blowing the token budget on low-relevance messages.
        total_messages = len(list(self.store.get_recent(10000)))  # fast count proxy
        if total_messages == 0:
            total_messages = 1  # avoid div-by-zero
        tag_counts = self.store.tag_counts()
        useful_tags = [
            t for t in inferred_tags
            if tag_counts.get(t, 0) / total_messages <= TOPIC_TAG_MAX_CORPUS_FREQ
        ]
        # Fall back to all tags if every tag is high-frequency (small corpus)
        if not useful_tags and inferred_tags:
            # Sort by ascending frequency and take the bottom half
            useful_tags = sorted(inferred_tags, key=lambda t: tag_counts.get(t, 0))
            useful_tags = useful_tags[: max(1, len(useful_tags) // 2)]

        for tag in useful_tags:
            for msg in self.store.get_by_tag(tag, limit=20):
                if msg.id not in seen_ids:
                    topic_candidates.append(msg)
                    seen_ids.add(msg.id)

        # newest-first within topic candidates, then pack to budget
        topic_candidates.sort(key=lambda m: m.timestamp, reverse=True)

        topic_msgs: List[Message] = []
        topic_tokens = 0

        for msg in topic_candidates:
            cost = _estimate_tokens(msg)
            if topic_tokens + cost > topic_budget:
                break
            topic_msgs.append(msg)
            topic_tokens += cost

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
