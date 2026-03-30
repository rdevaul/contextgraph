"""
harvester.py — Extract interaction pairs from OpenClaw session files.

Reads OpenClaw JSONL session logs, pairs user/assistant messages,
and appends new records to the tag-context interaction log.

Usage:
  python3 scripts/harvester.py [--since YYYY-MM-DD] [--session SESSION_KEY]
                               [--dry-run] [--verbose]

Defaults to harvesting the main session (agent:main:main) and all
direct Telegram sessions. Skips cron/hook sessions (noisy, low signal).
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Allow running from project root or scripts/ directory
sys.path.insert(0, str(Path(__file__).parent.parent))
from logger import log_interaction, iter_records, LOG_DIR

SESSIONS_DIR = Path.home() / ".openclaw/agents/main/sessions"
SESSIONS_INDEX = SESSIONS_DIR / "sessions.json"
STATE_FILE = Path(__file__).parent.parent / "data" / "harvester-state.json"

# Session key patterns to harvest (skip cron/hook/group sessions)
INCLUDE_PATTERNS = [
    "agent:main:main",          # primary DM session
    "agent:main:telegram:",     # Telegram DMs (includes main)
    "agent:main:voice",         # Voice PWA sessions
    "agent:main:discord:",      # Discord DM sessions
    "agent:main:direct:",       # Discord direct sessions (alternate pattern)
    "agent:vera:",              # Vera subagent sessions
]

EXCLUDE_PATTERNS = [
    ":cron:",
    ":hook:",
    ":group:",                  # group chat sessions (noisier, may include private data)
]


def _channel_from_key(session_key: str) -> str:
    if "telegram" in session_key:
        return "telegram"
    if "voice" in session_key:
        return "voice-pwa"
    if "discord" in session_key or "direct:" in session_key:
        return "discord"
    if "console" in session_key:
        return "console"
    if "vera" in session_key:
        return "vera"
    return "main"


def _user_id_from_key(session_key: str) -> str:
    """Best-effort user ID extraction from session key."""
    parts = session_key.split(":")
    # agent:main:telegram:<user_id> → "<user_id>"
    for part in reversed(parts):
        if part.isdigit():
            return part
    return "unknown"


def _extract_text(message: dict) -> str:
    """Extract plain text from an OpenClaw message dict."""
    content = message.get("content", "")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = "\n".join(parts).strip()
    else:
        return ""
    return _clean_user_text(text)


def _clean_user_text(text: str) -> str:
    """Strip OpenClaw metadata envelopes and prefixes from user messages."""
    import re
    # Strip "Sender (untrusted metadata): ```json ... ```"
    text = re.sub(
        r"Sender \(untrusted metadata\):\s*```(?:json)?\s*\{.*?\}\s*```\s*",
        "", text, flags=re.DOTALL
    )
    # Strip "Conversation info (untrusted metadata): ```json ... ```"
    text = re.sub(
        r"Conversation info \(untrusted metadata\):\s*```(?:json)?\s*\{.*?\}\s*```\s*",
        "", text, flags=re.DOTALL
    )
    # Strip "Replied message (untrusted, for context): ```json ... ```"
    text = re.sub(
        r"Replied message \(untrusted.*?\):\s*```(?:json)?\s*\{.*?\}\s*```\s*",
        "", text, flags=re.DOTALL
    )
    # Strip voice PWA timestamp prefix: "[Mon 2026-02-23 07:59 PST] [Voice PWA] "
    text = re.sub(r"^\[.*?\]\s*\[Voice PWA\]\s*", "", text)
    # Strip system message prefix
    text = re.sub(r"^\[.*?\]\s*\[System Message\].*?\n", "", text, flags=re.DOTALL)
    # Strip queued messages block
    text = re.sub(r"\[Queued messages while agent was busy\].*", "", text, flags=re.DOTALL)
    return text.strip()


def _channel_from_text(user_text: str, session_key: str) -> str:
    """Detect channel from message content or session key."""
    import re
    if re.search(r"\[Voice PWA\]", user_text):
        return "voice-pwa"
    return _channel_from_key(session_key)


def harvest_session(session_key: str, session_meta: dict,
                    since_ts: float, dry_run: bool, verbose: bool) -> int:
    """Harvest one session file. Returns count of records logged."""
    session_id = session_meta.get("sessionId")
    if not session_id:
        return 0

    jsonl_path = SESSIONS_DIR / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return 0

    channel = _channel_from_key(session_key)
    user_id = _user_id_from_key(session_key)

    # Parse all messages in the session, in order
    messages = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "message":
                continue
            msg = entry.get("message", {})
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            ts = entry.get("timestamp", 0)
            if isinstance(ts, (int, float)):
                ts = ts / 1000.0 if ts > 1e10 else ts  # ms → s if needed
            else:
                ts = time.time()
            text = _extract_text(msg)
            if text:
                messages.append({"role": role, "text": text, "ts": ts})

    if not messages:
        return 0

    # Pair user → assistant messages
    logged = 0
    i = 0
    while i < len(messages) - 1:
        if messages[i]["role"] == "user" and messages[i + 1]["role"] == "assistant":
            user_msg = messages[i]
            asst_msg = messages[i + 1]
            # Skip if before since_ts
            if asst_msg["ts"] <= since_ts:
                i += 2
                continue
            # Skip system messages and heartbeats
            text = user_msg["text"]
            if any(pat in text for pat in [
                "HEARTBEAT_OK", "Pre-compaction memory flush",
                "[cron:", "[System Message]", "Post-Compaction Audit"
            ]):
                i += 2
                continue
            if verbose:
                print(f"  [{session_key[:40]}] {user_msg['text'][:60]!r} → {asst_msg['text'][:40]!r}")
            if not dry_run:
                log_interaction(
                    user_text=user_msg["text"],
                    assistant_text=asst_msg["text"],
                    session_id=session_key,
                    user_id=user_id,
                    channel=_channel_from_text(user_msg["text"], session_key),
                    interaction_at=user_msg["ts"],
                )
            logged += 1
            i += 2
        else:
            i += 1

    return logged


def load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {"last_harvest_ts": 0.0}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest OpenClaw sessions → interaction log")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="Harvest from this date")
    parser.add_argument("--session", help="Harvest only this session key")
    parser.add_argument("--dry-run", action="store_true", help="Print but don't write")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    state = load_state()

    if args.since:
        import datetime
        since_dt = datetime.datetime.strptime(args.since, "%Y-%m-%d")
        since_ts = since_dt.timestamp()
    else:
        since_ts = state["last_harvest_ts"]

    if not SESSIONS_INDEX.exists():
        print(f"Sessions index not found: {SESSIONS_INDEX}", file=sys.stderr)
        sys.exit(1)

    with SESSIONS_INDEX.open() as f:
        sessions = json.load(f)

    total = 0
    harvest_ts = time.time()

    for session_key, session_meta in sessions.items():
        # Filter by --session flag
        if args.session and args.session not in session_key:
            continue
        # Apply include/exclude patterns
        if not args.session:
            if not any(pat in session_key for pat in INCLUDE_PATTERNS):
                continue
            if any(pat in session_key for pat in EXCLUDE_PATTERNS):
                continue

        if args.verbose:
            print(f"Harvesting: {session_key}")

        count = harvest_session(session_key, session_meta,
                                since_ts=since_ts,
                                dry_run=args.dry_run,
                                verbose=args.verbose)
        if count:
            print(f"  {session_key[:50]}: {count} interactions")
        total += count

    print(f"\nTotal logged: {total}")
    if not args.dry_run:
        state["last_harvest_ts"] = harvest_ts
        save_state(state)
        print(f"State saved (next harvest will start from {time.strftime('%Y-%m-%d %H:%M', time.localtime(harvest_ts))})")


if __name__ == "__main__":
    main()
