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
    external_id: Optional[str] = None  # OpenClaw AgentMessage.id or other external system ID
    summary: Optional[str] = None      # Summarized version for large messages
    is_automated: bool = False         # True for cron jobs, heartbeats, etc.
    channel_label: Optional[str] = None  # Channel label for per-agent memory isolation

    @classmethod
    def new(cls, session_id: str, user_id: str, timestamp: float,
            user_text: str, assistant_text: str,
            tags: Optional[List[str]] = None, token_count: int = 0,
            external_id: Optional[str] = None, is_automated: bool = False,
            channel_label: Optional[str] = None) -> "Message":
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
            external_id=external_id,
            is_automated=is_automated,
            channel_label=channel_label,
        )


class MessageStore:
    """
    SQLite-backed store for messages and their tag associations.

    Tags are stored in a normalized `tags` table; the `messages` table
    does not duplicate them. All tag operations go through the tags table.

    Threading model: single shared connection (check_same_thread=False) protected
    by a reentrant lock. WAL mode allows concurrent readers; the lock serializes
    writers to prevent "database is locked" under concurrent test/request load.
    """

    DEFAULT_DB = Path.home() / ".tag-context" / "store.db"

    def __init__(self, db_path: Optional[str] = None) -> None:
        path = Path(db_path) if db_path else self.DEFAULT_DB
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(path)
        self._lock = threading.RLock()
        self._conn_obj: Optional[sqlite3.Connection] = None
        self._init_db()

    # ── connection ────────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        """Return the shared SQLite connection (thread-safe via _lock)."""
        if self._conn_obj is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._conn_obj = conn
        return self._conn_obj

    # ── migrations ────────────────────────────────────────────────────────────

    MIGRATIONS = {
        2: """
            ALTER TABLE messages ADD COLUMN external_id TEXT;
            CREATE INDEX IF NOT EXISTS idx_messages_external_id ON messages(external_id);
        """,
        3: """
            ALTER TABLE messages ADD COLUMN summary TEXT;
        """,
        4: """
            ALTER TABLE messages ADD COLUMN is_automated INTEGER NOT NULL DEFAULT 0;
        """,
        5: """
            ALTER TABLE messages ADD COLUMN channel_label TEXT;
            CREATE INDEX IF NOT EXISTS idx_messages_channel_label ON messages(channel_label);
        """,
    }

    def _init_db(self) -> None:
        with self._lock:
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

                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL,
                    description TEXT
                );
            """)
            conn.commit()

            # Run pending migrations
            self._run_migrations(conn)

    def _get_schema_version(self, conn: sqlite3.Connection) -> int:
        """Get current schema version (0 if schema_version table doesn't exist yet)."""
        try:
            row = conn.execute(
                "SELECT MAX(version) as v FROM schema_version"
            ).fetchone()
            return row["v"] if row["v"] is not None else 0
        except sqlite3.OperationalError:
            # schema_version table doesn't exist yet (very old DB)
            return 0

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply any pending migrations."""
        import time

        current_version = self._get_schema_version(conn)

        for version in sorted(self.MIGRATIONS.keys()):
            if version <= current_version:
                continue

            migration_sql = self.MIGRATIONS[version]

            try:
                # Execute migration (handle duplicate column errors gracefully)
                for statement in migration_sql.strip().split(';'):
                    statement = statement.strip()
                    if statement:
                        try:
                            conn.execute(statement)
                        except sqlite3.OperationalError as e:
                            # Idempotent: ignore "duplicate column" errors
                            if "duplicate column" not in str(e).lower():
                                raise

                # Record migration
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
                    (version, time.time(), f"Migration v{version}")
                )
                conn.commit()

            except Exception as e:
                conn.rollback()
                raise RuntimeError(f"Migration {version} failed: {e}")

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
            external_id=row["external_id"] if "external_id" in row.keys() else None,
            summary=row["summary"] if "summary" in row.keys() else None,
            is_automated=bool(row["is_automated"]) if "is_automated" in row.keys() else False,
            channel_label=row["channel_label"] if "channel_label" in row.keys() else None,
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
        with self._lock:
            conn = self._conn()
            conn.execute(
                """INSERT INTO messages (id, session_id, user_id, timestamp,
                   user_text, assistant_text, token_count, external_id, is_automated,
                   channel_label)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (msg.id, msg.session_id, msg.user_id, msg.timestamp,
                 msg.user_text, msg.assistant_text, msg.token_count, msg.external_id,
                 1 if msg.is_automated else 0, msg.channel_label),
            )
            for tag in msg.tags:
                conn.execute(
                    "INSERT OR IGNORE INTO tags (message_id, tag) VALUES (?, ?)",
                    (msg.id, tag),
                )
            conn.commit()

    def add_tags(self, message_id: str, tags: List[str]) -> None:
        """Add tags to an existing message (idempotent)."""
        with self._lock:
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

    def get_recent(self, n: int, include_automated: bool = False) -> List[Message]:
        """Return the N most recent messages, newest first.

        Parameters
        ----------
        n : int
            Number of messages to return
        include_automated : bool
            If False (default), exclude automated turns (cron/heartbeat/etc)
        """
        conn = self._conn()

        if include_automated:
            query = "SELECT * FROM messages ORDER BY timestamp DESC LIMIT ?"
        else:
            query = "SELECT * FROM messages WHERE is_automated = 0 ORDER BY timestamp DESC LIMIT ?"

        rows = conn.execute(query, (n,)).fetchall()
        ids = [r["id"] for r in rows]
        tags_map = self._fetch_tags_bulk(conn, ids)
        return [self._row_to_message(r, tags_map[r["id"]]) for r in rows]

    def get_recent_by_session(self, n: int, session_id: str) -> List[Message]:
        """Return the N most recent messages for a specific session, newest first."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
            (session_id, n)
        ).fetchall()
        ids = [r["id"] for r in rows]
        tags_map = self._fetch_tags_bulk(conn, ids)
        return [self._row_to_message(r, tags_map[r["id"]]) for r in rows]

    def get_by_tag(self, tag: str, limit: int = 20, include_automated: bool = False) -> List[Message]:
        """Return messages carrying `tag`, newest first.

        Parameters
        ----------
        tag : str
            Tag to filter by
        limit : int
            Maximum number of messages to return
        include_automated : bool
            If False (default), exclude automated turns (cron/heartbeat/etc)
        """
        conn = self._conn()

        if include_automated:
            query = """SELECT m.* FROM messages m
                       INNER JOIN tags t ON m.id = t.message_id
                       WHERE t.tag = ?
                       ORDER BY m.timestamp DESC
                       LIMIT ?"""
        else:
            query = """SELECT m.* FROM messages m
                       INNER JOIN tags t ON m.id = t.message_id
                       WHERE t.tag = ? AND m.is_automated = 0
                       ORDER BY m.timestamp DESC
                       LIMIT ?"""

        rows = conn.execute(query, (tag, limit)).fetchall()
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

    def get_by_external_id(self, external_id: str) -> Optional[Message]:
        """Fetch a single message by external_id, or None if not found."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM messages WHERE external_id = ?", (external_id,)
        ).fetchone()
        if row is None:
            return None
        tags = self._fetch_tags_for(conn, row["id"])
        return self._row_to_message(row, tags)

    def get_by_external_ids(self, external_ids: List[str]) -> List[Message]:
        """Fetch messages by external_ids. Returns list in same order as input, skipping missing IDs."""
        if not external_ids:
            return []
        conn = self._conn()
        placeholders = ",".join("?" * len(external_ids))
        rows = conn.execute(
            f"SELECT * FROM messages WHERE external_id IN ({placeholders})",
            external_ids,
        ).fetchall()
        ids = [r["id"] for r in rows]
        tags_map = self._fetch_tags_bulk(conn, ids)
        # Build a map from external_id to Message
        msg_by_ext_id = {r["external_id"]: self._row_to_message(r, tags_map[r["id"]]) for r in rows}
        # Return in the same order as input, skipping missing
        return [msg_by_ext_id[eid] for eid in external_ids if eid in msg_by_ext_id]

    def count(self) -> int:
        """Return exact total number of messages in the store."""
        with self._get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def get_non_automated(self, limit: int = 1000) -> List[Message]:
        """Return non-automated messages (excluding cron/heartbeat turns), newest first."""
        conn = self._conn()
        rows = conn.execute(
            """SELECT * FROM messages
               WHERE is_automated = 0
               ORDER BY timestamp DESC
               LIMIT ?""",
            (limit,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        tags_map = self._fetch_tags_bulk(conn, ids)
        return [self._row_to_message(r, tags_map[r["id"]]) for r in rows]

    # ── summary ───────────────────────────────────────────────────────────────

    def get_summary(self, message_id: str) -> Optional[str]:
        """Fetch the summary for a message by ID, or None if not set."""
        conn = self._conn()
        row = conn.execute(
            "SELECT summary FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        return row["summary"]

    def set_summary(self, message_id: str, summary: str) -> None:
        """Store a summary for a message."""
        conn = self._conn()
        conn.execute(
            "UPDATE messages SET summary = ? WHERE id = ?",
            (summary, message_id),
        )
        conn.commit()
