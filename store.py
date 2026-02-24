"""
store.py — SQLite-backed message store and tag index for the tag-context system.
"""

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Message:
    """A single user/assistant exchange with associated tags."""
    id: str
    session_id: str
    user_id: str
    timestamp: float          # Unix timestamp
    user_text: str
    assistant_text: str
    tags: List[str] = field(default_factory=list)
    token_count: int = 0

    @classmethod
    def new(cls, session_id: str, user_id: str, timestamp: float,
            user_text: str, assistant_text: str,
            tags: Optional[List[str]] = None, token_count: int = 0) -> "Message":
        """Create a new Message with a generated UUID."""
        return cls(
            id=str(uuid.uuid4()),
            session_id=session_id,
            user_id=user_id,
            timestamp=timestamp,
            user_text=user_text,
            assistant_text=assistant_text,
            tags=tags or [],
            token_count=token_count,
        )


class MessageStore:
    """
    SQLite-backed store for messages and their tag associations.

    Tags are stored in a normalized `tags` table; the `messages` table
    does not duplicate them. All tag operations go through the tags table.
    """

    DEFAULT_DB = Path.home() / ".tag-context" / "store.db"

    def __init__(self, db_path: Optional[str] = None) -> None:
        path = Path(db_path) if db_path else self.DEFAULT_DB
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(path)
        self._local = threading.local()
        self._init_db()

    # ── connection ────────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        """Return a thread-local SQLite connection."""
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id            TEXT PRIMARY KEY,
                session_id    TEXT NOT NULL,
                user_id       TEXT NOT NULL,
                timestamp     REAL NOT NULL,
                user_text     TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                token_count   INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC);

            CREATE TABLE IF NOT EXISTS tags (
                message_id TEXT NOT NULL
                    REFERENCES messages(id) ON DELETE CASCADE,
                tag        TEXT NOT NULL,
                PRIMARY KEY (message_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
        """)
        conn.commit()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _row_to_message(self, row: sqlite3.Row, tags: List[str]) -> Message:
        return Message(
            id=row["id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            timestamp=row["timestamp"],
            user_text=row["user_text"],
            assistant_text=row["assistant_text"],
            tags=tags,
            token_count=row["token_count"],
        )

    def _fetch_tags_for(self, conn: sqlite3.Connection, message_id: str) -> List[str]:
        rows = conn.execute(
            "SELECT tag FROM tags WHERE message_id = ? ORDER BY tag", (message_id,)
        ).fetchall()
        return [r["tag"] for r in rows]

    def _fetch_tags_bulk(self, conn: sqlite3.Connection,
                         message_ids: List[str]) -> dict:
        """Return {message_id: [tags]} for a list of IDs."""
        if not message_ids:
            return {}
        placeholders = ",".join("?" * len(message_ids))
        rows = conn.execute(
            f"SELECT message_id, tag FROM tags WHERE message_id IN ({placeholders}) ORDER BY tag",
            message_ids,
        ).fetchall()
        result: dict = {mid: [] for mid in message_ids}
        for r in rows:
            result[r["message_id"]].append(r["tag"])
        return result

    # ── write ─────────────────────────────────────────────────────────────────

    def add_message(self, msg: Message) -> None:
        """Persist a message and its initial tags."""
        conn = self._conn()
        conn.execute(
            """INSERT INTO messages (id, session_id, user_id, timestamp,
               user_text, assistant_text, token_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (msg.id, msg.session_id, msg.user_id, msg.timestamp,
             msg.user_text, msg.assistant_text, msg.token_count),
        )
        for tag in msg.tags:
            conn.execute(
                "INSERT OR IGNORE INTO tags (message_id, tag) VALUES (?, ?)",
                (msg.id, tag),
            )
        conn.commit()

    def add_tags(self, message_id: str, tags: List[str]) -> None:
        """Add tags to an existing message (idempotent)."""
        conn = self._conn()
        for tag in tags:
            conn.execute(
                "INSERT OR IGNORE INTO tags (message_id, tag) VALUES (?, ?)",
                (message_id, tag),
            )
        conn.commit()

    # ── read ──────────────────────────────────────────────────────────────────

    def get_by_id(self, message_id: str) -> Optional[Message]:
        """Fetch a single message by ID, or None if not found."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        tags = self._fetch_tags_for(conn, message_id)
        return self._row_to_message(row, tags)

    def get_recent(self, n: int) -> List[Message]:
        """Return the N most recent messages, newest first."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY timestamp DESC LIMIT ?", (n,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        tags_map = self._fetch_tags_bulk(conn, ids)
        return [self._row_to_message(r, tags_map[r["id"]]) for r in rows]

    def get_by_tag(self, tag: str, limit: int = 20) -> List[Message]:
        """Return messages carrying `tag`, newest first."""
        conn = self._conn()
        rows = conn.execute(
            """SELECT m.* FROM messages m
               INNER JOIN tags t ON m.id = t.message_id
               WHERE t.tag = ?
               ORDER BY m.timestamp DESC
               LIMIT ?""",
            (tag, limit),
        ).fetchall()
        ids = [r["id"] for r in rows]
        tags_map = self._fetch_tags_bulk(conn, ids)
        return [self._row_to_message(r, tags_map[r["id"]]) for r in rows]

    def get_all_tags(self) -> List[str]:
        """Return all distinct tags in the index, alphabetically."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT DISTINCT tag FROM tags ORDER BY tag"
        ).fetchall()
        return [r["tag"] for r in rows]

    def tag_counts(self) -> dict:
        """Return {tag: message_count} for all tags."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT tag, COUNT(*) as cnt FROM tags GROUP BY tag ORDER BY cnt DESC"
        ).fetchall()
        return {r["tag"]: r["cnt"] for r in rows}
