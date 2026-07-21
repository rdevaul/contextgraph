"""
test_threadlocal_concurrency.py — regression test for the 2026-07-21 fix.

Reproduces the concurrent-write pattern that produced intermittent
"cannot commit - no transaction is active" errors and dropped summary
writes when MessageStore shared a single sqlite3.Connection across
FastAPI's threadpool.

With thread-local connections, N threads each doing interleaved
add_message / set_summary / read must all succeed with no exceptions
and every summary must actually land.
"""

import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from store import MessageStore, Message  # noqa: E402


def _mk_msg(i: int) -> Message:
    return Message.new(
        session_id=f"sess-{i % 4}",
        user_id="tester",
        timestamp=time.time(),
        user_text=f"user text {i}",
        assistant_text=f"assistant text {i} " + ("x" * 50),
        tags=["t:test"],
        token_count=100,
        channel_label="rich",
        subchannel_label=f"pane-{i % 3}",
    )


def test_concurrent_writes_and_summaries(tmp_path):
    db = str(tmp_path / "concurrency.db")
    store = MessageStore(db_path=db)

    n_threads = 8
    per_thread = 40
    errors: list[Exception] = []
    ids_by_thread: dict[int, list[str]] = {}
    lock = threading.Lock()

    def worker(tid: int) -> None:
        local_ids = []
        try:
            for k in range(per_thread):
                m = _mk_msg(tid * 1000 + k)
                store.add_message(m)
                local_ids.append(m.id)
                # Interleave a summary write (the operation that used to race
                # on the shared connection's transaction state) and a read.
                store.set_summary(m.id, f"summary for {m.id}")
                _ = store.get_by_id(m.id)
                _ = store.get_recent(5, channel_label="rich")
        except Exception as e:  # pragma: no cover - failure path
            with lock:
                errors.append(e)
        finally:
            with lock:
                ids_by_thread[tid] = local_ids

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 1. No transaction / connection errors of any kind.
    assert not errors, f"concurrent workers raised: {errors[:3]}"

    # 2. Every message persisted AND has its summary (no silent drops).
    all_ids = [mid for ids in ids_by_thread.values() for mid in ids]
    assert len(all_ids) == n_threads * per_thread
    missing_summary = []
    for mid in all_ids:
        s = store.get_summary(mid)
        if not s or not s.strip():
            missing_summary.append(mid)
    assert not missing_summary, (
        f"{len(missing_summary)}/{len(all_ids)} rows lost their summary "
        f"(the pre-fix failure mode)"
    )


def test_subchannel_resolver_threadlocal(tmp_path):
    """Resolver must write correctly from many threads via the connection
    factory (store._conn), which returns each thread's own connection."""
    from subchannel_resolver import SubchannelResolver

    db = str(tmp_path / "resolver.db")
    store = MessageStore(db_path=db)
    resolver = SubchannelResolver(store._conn)  # pass the factory (bound method)

    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(tid: int) -> None:
        try:
            for k in range(30):
                label = f"Pane {tid}-{k}"
                canon = resolver.resolve(label)
                assert canon  # returns something
        except Exception as e:  # pragma: no cover
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"resolver workers raised: {errors[:3]}"
