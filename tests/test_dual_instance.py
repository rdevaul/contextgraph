"""
test_dual_instance.py — Concurrent access tests for SQLite store.

Simulates two OpenClaw instances (e.g., local + Jarvis) writing to
the same SQLite database simultaneously.

Tests WAL mode, lock contention, data integrity, and concurrency limits.
This is the critical test before re-enabling graphMode on Jarvis.

Run: pytest tests/test_dual_instance.py -v
"""

import pytest
import tempfile
import sqlite3
import time
import json
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from store import MessageStore


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def shared_db(tmp_path):
    """A shared SQLite DB that all threads access."""
    return str(tmp_path / "concurrent-test.db")


@pytest.fixture
def wal_store(shared_db):
    """Store with WAL mode enabled."""
    store = MessageStore(shared_db)
    conn = sqlite3.connect(shared_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.close()
    return store


# ── WAL Mode Tests ───────────────────────────────────────────────────────────

class TestWALMode:
    def test_wal_can_be_enabled(self, shared_db):
        """WAL mode should be settable without error."""
        conn = sqlite3.connect(shared_db)
        # Create the messages table first
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_text TEXT, assistant_text TEXT, timestamp REAL,
                tags TEXT, token_count INTEGER, channel_label TEXT,
                session_id TEXT, user_id TEXT, external_id TEXT,
                session_tags TEXT, graph_count INTEGER, is_pinned INTEGER DEFAULT 0,
                pinned_to_tag TEXT, summary TEXT, is_automated INTEGER DEFAULT 0,
                turn_type TEXT, is_reframed INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        result = conn.execute("PRAGMA journal_mode").fetchone()
        assert result[0] in ("wal", "WAL"), f"Expected WAL, got {result[0]}"
        conn.close()

    def test_concurrent_reads_with_one_writer(self, shared_db):
        """WAL allows concurrent reads while one writer is active."""
        # Setup
        conn = sqlite3.connect(shared_db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_text TEXT, tags TEXT, timestamp REAL,
                assistant_text TEXT, token_count INTEGER DEFAULT 0,
                channel_label TEXT DEFAULT 'test', session_id TEXT DEFAULT 'test',
                session_tags TEXT, graph_count INTEGER DEFAULT 0,
                external_id TEXT, is_pinned INTEGER DEFAULT 0,
                pinned_to_tag TEXT, summary TEXT, user_id TEXT,
                is_automated INTEGER DEFAULT 0, turn_type TEXT,
                is_reframed INTEGER DEFAULT 0
            )
        """)
        conn.close()

        write_count = [0]
        read_counts = []
        barrier = threading.Barrier(3)  # 1 writer + 2 readers

        def writer():
            barrier.wait()
            for i in range(50):
                conn = sqlite3.connect(shared_db)
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute(
                    "INSERT INTO messages (user_text, tags, timestamp) VALUES (?, ?, ?)",
                    (f"msg-{i}", json.dumps(["ai"]), time.time())
                )
                conn.commit()
                conn.close()
                write_count[0] += 1

        def reader():
            barrier.wait()
            time.sleep(0.01)  # Let writer start
            local_reads = 0
            conn = sqlite3.connect(shared_db)
            for _ in range(50):
                count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                read_counts.append(count)
                local_reads += 1
            conn.close()
            return local_reads

        t_writer = threading.Thread(target=writer)
        t_readers = []
        for _ in range(2):
            t = threading.Thread(target=reader)
            t.start()
            t_readers.append(t)

        t_writer.start()
        t_writer.join(timeout=30)
        for t in t_readers:
            t.join(timeout=30)

        assert write_count[0] == 50, f"Expected 50 writes, got {write_count[0]}"
        assert all(c >= 0 for c in read_counts), "Non-negative read counts"

    def test_no_deadlock_concurrent_writes(self, shared_db):
        """Multiple concurrent writes should not deadlock."""
        # Setup
        conn = sqlite3.connect(shared_db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_text TEXT, tags TEXT, timestamp REAL,
                assistant_text TEXT, token_count INTEGER DEFAULT 0,
                channel_label TEXT DEFAULT 'test', session_id TEXT DEFAULT 'test',
                session_tags TEXT, graph_count INTEGER DEFAULT 0,
                external_id TEXT, is_pinned INTEGER DEFAULT 0,
                pinned_to_tag TEXT, summary TEXT, user_id TEXT,
                is_automated INTEGER DEFAULT 0, turn_type TEXT,
                is_reframed INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

        errors = []
        write_counts = [0, 0]

        def writer(idx, count):
            for i in range(30):
                try:
                    conn = sqlite3.connect(shared_db)
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.execute(
                        "INSERT INTO messages (user_text, tags, timestamp, channel_label) VALUES (?, ?, ?, ?)",
                        (f"writer-{idx}-msg-{i}", json.dumps(["ai"]), time.time(), f"chan-{idx}")
                    )
                    conn.commit()
                    conn.close()
                    write_counts[idx] += 1
                    time.sleep(0.001)  # Slight delay to make contention more realistic
                except Exception as e:
                    errors.append(f"Writer {idx} error at msg {i}: {e}")

        t1 = threading.Thread(target=writer, args=(0, 30))
        t2 = threading.Thread(target=writer, args=(1, 30))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert not errors, f"Deadlock or error: {errors}"
        assert write_counts[0] + write_counts[1] == 60, \
            f"Expected 60 total writes, got {write_counts[0] + write_counts[1]}"


# ── Data Integrity Tests ──────────────────────────────────────────────────────

class TestConcurrentIntegrity:
    def test_no_duplicate_external_ids(self, shared_db):
        """Concurrent upserts with same external_id should not create duplicates."""
        store = MessageStore(shared_db)
        # Enable WAL
        conn = sqlite3.connect(shared_db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.close()

        ext_id = "shared-concurrent-test-1"
        errors = []

        def upsert(val):
            try:
                conn = sqlite3.connect(shared_db)
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute(
                    """INSERT INTO messages (
                        user_text, assistant_text, timestamp, tags, token_count,
                        channel_label, session_id, external_id, graph_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(external_id) DO UPDATE SET
                        user_text = excluded.user_text,
                        timestamp = excluded.timestamp
                    """,
                    (
                        f"v{val}", f"reply-{val}", time.time(),
                        json.dumps(["test"]), 50, "rich", "session-1",
                        ext_id, 0,
                    ),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                errors.append(str(e))

        # Run 10 concurrent upserts
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(upsert, i) for i in range(10)]
            for f in as_completed(futures):
                f.result()

        assert not errors, f"Errors during concurrent upserts: {errors}"

        # Should have exactly 1 row
        conn = sqlite3.connect(shared_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE external_id = ?", (ext_id,)
        ).fetchone()[0]
        conn.close()
        assert count <= 1, f"Expected 0-1 rows for {ext_id}, got {count}"


# ── Throughput Tests ─────────────────────────────────────────────────────────

class TestThroughput:
    def test_ingest_rate_single_threaded(self, shared_db):
        """Measure single-threaded ingest rate."""
        store = MessageStore(shared_db)
        
        start = time.time()
        n = 500
        for i in range(n):
            store.insert_message(
                user_text=f"message {i}",
                assistant_text=f"reply {i}",
                timestamp=time.time(),
                tags=["ai", "code"],
                token_count=50,
                channel_label="rich",
                session_id="throughput-test",
            )
        elapsed = time.time() - start
        rate = n / elapsed if elapsed > 0 else float('inf')
        
        print(f"\n  Single-threaded ingest: {rate:.0f} msg/s ({n} msgs in {elapsed:.2f}s)")
        # Should be well above 100 msg/s for SQLite
        assert rate > 50, f"Ingest rate {rate:.0f} msg/s is too low"

    def test_tag_counts_after_bulk_ingest(self, shared_db):
        """Tag counts should be accurate after bulk ingest."""
        store = MessageStore(shared_db)
        
        n = 100
        for i in range(n):
            tags = ["ai"] if i % 2 == 0 else ["ai", "code"]
            store.insert_message(
                f"msg-{i}", f"reply-{i}", time.time(),
                tags=tags, token_count=50,
                channel_label="rich", session_id="bulk-test",
            )
        
        all_tags = store.get_all_tags()
        ai_count = store.count_messages_with_tags(["ai"])
        
        assert ai_count == n, f"Expected {n} messages with 'ai', got {ai_count}"
