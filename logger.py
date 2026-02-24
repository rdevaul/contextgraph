"""
logger.py — Interaction logger for the tag-context system.

Appends message/response pairs to daily JSONL files.
Tags are intentionally excluded at log time; replay.py assigns them
via the tagger, allowing re-tagging with evolved strategies.

Log format (one JSON object per line):
{
  "id":             str (uuid4),
  "logged_at":      float (unix timestamp of logging),
  "session_id":     str,
  "user_id":        str,
  "channel":        str,          # "telegram", "voice-pwa", "console", etc.
  "interaction_at": float,        # when the exchange actually happened
  "user_text":      str,
  "assistant_text": str,
  "token_count":    int           # estimated
}
"""

import json
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


LOG_DIR = Path(__file__).parent / "data" / "interactions"


@dataclass
class InteractionRecord:
    id: str
    logged_at: float
    session_id: str
    user_id: str
    channel: str
    interaction_at: float
    user_text: str
    assistant_text: str
    token_count: int


def _log_path(ts: float) -> Path:
    """Return the JSONL path for a given unix timestamp."""
    import datetime
    date = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    return LOG_DIR / f"{date}.jsonl"


def log_interaction(
    user_text: str,
    assistant_text: str,
    session_id: str = "default",
    user_id: str = "unknown",
    channel: str = "unknown",
    interaction_at: Optional[float] = None,
    token_count: Optional[int] = None,
) -> InteractionRecord:
    """
    Append one interaction to today's JSONL log.

    Parameters
    ----------
    user_text       The user's message.
    assistant_text  The assistant's response.
    session_id      OpenClaw session key or similar.
    user_id         Sender ID (Telegram user ID, etc.)
    channel         Source channel: "telegram", "voice-pwa", "console", etc.
    interaction_at  Unix timestamp of the exchange (defaults to now).
    token_count     Estimated tokens; computed from word count if omitted.
    """
    now = time.time()
    if interaction_at is None:
        interaction_at = now
    if token_count is None:
        words = len((user_text + " " + assistant_text).split())
        token_count = max(1, int(words * 1.3))

    record = InteractionRecord(
        id=str(uuid.uuid4()),
        logged_at=now,
        session_id=session_id,
        user_id=user_id,
        channel=channel,
        interaction_at=interaction_at,
        user_text=user_text,
        assistant_text=assistant_text,
        token_count=token_count,
    )

    path = _log_path(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record)) + "\n")

    return record


def iter_records(start_date: Optional[str] = None,
                 end_date: Optional[str] = None):
    """
    Iterate over all InteractionRecords in the log directory.

    Parameters
    ----------
    start_date  "YYYY-MM-DD" inclusive lower bound (optional)
    end_date    "YYYY-MM-DD" inclusive upper bound (optional)
    """
    paths = sorted(LOG_DIR.glob("*.jsonl"))
    for path in paths:
        date_str = path.stem            # "2026-02-24"
        if start_date and date_str < start_date:
            continue
        if end_date and date_str > end_date:
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield InteractionRecord(**json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    continue  # skip malformed lines


def count_records(start_date: Optional[str] = None,
                  end_date: Optional[str] = None) -> int:
    """Count log records in the given date range."""
    return sum(1 for _ in iter_records(start_date, end_date))
