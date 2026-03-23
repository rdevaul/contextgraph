"""Tests for store.py"""
import tempfile, time, pytest
from store import Message, MessageStore


@pytest.fixture
def store(tmp_path):
    return MessageStore(db_path=str(tmp_path / "test.db"))


def make_msg(**kwargs):
    defaults = dict(session_id="s1", user_id="u1", timestamp=time.time(),
                    user_text="hello", assistant_text="world", tags=[], token_count=10)
    defaults.update(kwargs)
    return Message.new(**defaults)


def test_add_and_get_by_id(store):
    msg = make_msg(user_text="test message")
    store.add_message(msg)
    retrieved = store.get_by_id(msg.id)
    assert retrieved is not None
    assert retrieved.id == msg.id
    assert retrieved.user_text == "test message"


def test_get_recent_order(store):
    for i in range(5):
        store.add_message(make_msg(timestamp=float(i), user_text=f"msg {i}"))
    recent = store.get_recent(3)
    assert len(recent) == 3
    assert recent[0].timestamp > recent[1].timestamp  # newest first


def test_tags_stored_and_retrieved(store):
    msg = make_msg(tags=["security", "networking"])
    store.add_message(msg)
    retrieved = store.get_by_id(msg.id)
    assert "security" in retrieved.tags
    assert "networking" in retrieved.tags


def test_get_by_tag(store):
    msg1 = make_msg(tags=["security"], user_text="secure thing")
    msg2 = make_msg(tags=["networking"], user_text="network thing")
    store.add_message(msg1)
    store.add_message(msg2)
    results = store.get_by_tag("security")
    assert len(results) == 1
    assert results[0].id == msg1.id


def test_add_tags(store):
    msg = make_msg(tags=["security"])
    store.add_message(msg)
    store.add_tags(msg.id, ["networking", "security"])  # security is duplicate
    retrieved = store.get_by_id(msg.id)
    assert "networking" in retrieved.tags
    assert retrieved.tags.count("security") == 1  # no dupes


def test_get_all_tags(store):
    store.add_message(make_msg(tags=["a", "b"]))
    store.add_message(make_msg(tags=["b", "c"]))
    tags = store.get_all_tags()
    assert set(tags) == {"a", "b", "c"}


def test_tag_counts(store):
    store.add_message(make_msg(tags=["security"]))
    store.add_message(make_msg(tags=["security", "networking"]))
    counts = store.tag_counts()
    assert counts["security"] == 2
    assert counts["networking"] == 1


def test_external_id_field(store):
    """Test that external_id field works correctly."""
    msg = make_msg(external_id="ext-123", user_text="external message")
    store.add_message(msg)

    # Retrieve by external_id
    retrieved = store.get_by_external_id("ext-123")
    assert retrieved is not None
    assert retrieved.external_id == "ext-123"
    assert retrieved.user_text == "external message"


def test_is_automated_field(store):
    """Test that is_automated field works correctly."""
    auto_msg = make_msg(user_text="[cron:123] Task done", is_automated=True)
    normal_msg = make_msg(user_text="Normal message", is_automated=False)

    store.add_message(auto_msg)
    store.add_message(normal_msg)

    # get_recent should exclude automated by default
    recent = store.get_recent(10, include_automated=False)
    assert len(recent) == 1
    assert recent[0].id == normal_msg.id

    # get_recent with include_automated=True should include all
    recent_all = store.get_recent(10, include_automated=True)
    assert len(recent_all) == 2


def test_summary_field(store):
    """Test that summary field works correctly."""
    msg = make_msg(user_text="Long message that needs summarization")
    store.add_message(msg)

    # Set summary
    store.set_summary(msg.id, "Short summary")

    # Retrieve summary
    summary = store.get_summary(msg.id)
    assert summary == "Short summary"

    # Retrieve full message
    retrieved = store.get_by_id(msg.id)
    assert retrieved.summary == "Short summary"


def test_schema_version_tracking(store):
    """Test that schema migrations are tracked."""
    conn = store._conn()

    # Check that schema_version table exists
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    assert cursor.fetchone() is not None

    # Check that migrations were recorded
    cursor = conn.execute("SELECT version FROM schema_version ORDER BY version")
    versions = [row[0] for row in cursor.fetchall()]

    # Should have migrations 2, 3, 4 (external_id, summary, is_automated)
    assert 2 in versions
    assert 3 in versions
    assert 4 in versions


def test_migration_idempotency(tmp_path):
    """Test that migrations are idempotent (can be run multiple times)."""
    db_path = str(tmp_path / "test.db")

    # Create store and add a message
    store1 = MessageStore(db_path)
    msg = make_msg(user_text="test")
    store1.add_message(msg)

    # Close and reopen (triggers migration check again)
    store2 = MessageStore(db_path)
    retrieved = store2.get_by_id(msg.id)
    assert retrieved is not None
    assert retrieved.user_text == "test"

    # Should not crash or duplicate migrations
    conn = store2._conn()
    cursor = conn.execute("SELECT COUNT(*) as cnt FROM schema_version")
    # Should have exactly 4 migration records (one per version: 2, 3, 4, 5)
    assert cursor.fetchone()[0] == 4


def test_get_non_automated(store):
    """Test get_non_automated method."""
    auto_msg1 = make_msg(user_text="[cron:123] Task 1", is_automated=True)
    auto_msg2 = make_msg(user_text="HEARTBEAT_OK", is_automated=True)
    normal_msg1 = make_msg(user_text="Normal 1", is_automated=False)
    normal_msg2 = make_msg(user_text="Normal 2", is_automated=False)

    store.add_message(auto_msg1)
    store.add_message(normal_msg1)
    store.add_message(auto_msg2)
    store.add_message(normal_msg2)

    non_auto = store.get_non_automated(limit=10)
    assert len(non_auto) == 2
    assert all(not msg.is_automated for msg in non_auto)
