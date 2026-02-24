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
