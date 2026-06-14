"""Rate limiter flush: DB UPSERT, cleanup, and unique index."""

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _clean_rate_state():
    """Clear rate_limits DB and in-memory store between tests."""
    from smol_llm_proxy.database import get_db

    with get_db() as conn:
        conn.execute("DELETE FROM rate_limits")
    from smol_llm_proxy.rate_limiter import _rate_store

    _rate_store.clear()
    yield
    from smol_llm_proxy.database import get_db

    with get_db() as conn:
        conn.execute("DELETE FROM rate_limits")
    from smol_llm_proxy.rate_limiter import _rate_store

    _rate_store.clear()


def test_flush_upsert_accumulates(client, admin_key):
    """Multiple commits in the same window should accumulate via UPSERT."""
    from smol_llm_proxy.auth import create_api_key
    from smol_llm_proxy.database import get_db
    from smol_llm_proxy.rate_limiter import commit_rate, _flush_to_db

    result = create_api_key("flush-upsert-test")
    key_id = result["id"]

    # Commit multiple times in the same window
    commit_rate(key_id, 100)
    commit_rate(key_id, 200)
    commit_rate(key_id, 50)

    asyncio.run(_flush_to_db())

    with get_db() as conn:
        row = conn.execute(
            "SELECT request_count, token_sum FROM rate_limits WHERE key_id = ?",
            (key_id,),
        ).fetchone()

    assert row is not None
    assert row["request_count"] == 3, f"Expected 3 requests, got {row['request_count']}"
    assert row["token_sum"] == 350, f"Expected 350 tokens, got {row['token_sum']}"


def test_flush_upsert_increments_on_second_flush(client):
    """Second flush to the same window should UPSERT and increment."""
    from smol_llm_proxy.auth import create_api_key
    from smol_llm_proxy.database import get_db
    from smol_llm_proxy.rate_limiter import commit_rate, _flush_to_db

    result = create_api_key("flush-upsert-inc")
    key_id = result["id"]

    # First batch
    commit_rate(key_id, 10)
    asyncio.run(_flush_to_db())

    with get_db() as conn:
        row = conn.execute(
            "SELECT request_count FROM rate_limits WHERE key_id = ?",
            (key_id,),
        ).fetchone()
    assert row["request_count"] == 1

    # Second batch to the same window
    commit_rate(key_id, 20)
    asyncio.run(_flush_to_db())

    with get_db() as conn:
        row = conn.execute(
            "SELECT request_count FROM rate_limits WHERE key_id = ?",
            (key_id,),
        ).fetchone()
    assert row["request_count"] == 2, f"Expected 2 after UPSERT, got {row['request_count']}"


def test_flush_empty_store_does_nothing(client):
    """Flushing an empty store should not raise or create rows."""
    from smol_llm_proxy.database import get_db
    from smol_llm_proxy.rate_limiter import _flush_to_db

    with get_db() as conn:
        count_before = conn.execute("SELECT COUNT(*) as cnt FROM rate_limits").fetchone()["cnt"]

    asyncio.run(_flush_to_db())

    with get_db() as conn:
        count_after = conn.execute("SELECT COUNT(*) as cnt FROM rate_limits").fetchone()["cnt"]
    assert count_after == count_before


def test_flush_cleanup_old_rows(client):
    """DELETE WHERE window_start < now-65 should remove expired rows."""
    from smol_llm_proxy.auth import create_api_key
    from smol_llm_proxy.database import get_db, _get_connection
    from smol_llm_proxy.rate_limiter import commit_rate, _flush_to_db, get_db_path
    import time as _time

    result = create_api_key("flush-cleanup-test")
    key_id = result["id"]

    # Insert a row with an old window_start (300 seconds in the past) — bypasses commit_rate
    old_ws = int(_time.time()) - 300

    conn_direct = _get_connection(get_db_path())
    conn_direct.execute(
        "INSERT INTO rate_limits (key_id, window_start, request_count, token_sum) VALUES (?, ?, ?, ?)",
        (key_id, float(old_ws), 999, 9999),
    )
    conn_direct.commit()

    with get_db() as db_conn:
        count_before = db_conn.execute(
            "SELECT COUNT(*) as cnt FROM rate_limits WHERE window_start < ?",
            (_time.time() - 60.0,),
        ).fetchone()["cnt"]
    assert count_before == 1

    # Trigger flush (commit_rate populates store so _flush_to_db doesn't return early)
    commit_rate(key_id, 1)
    asyncio.run(_flush_to_db())

    with get_db() as db_conn:
        count_after = db_conn.execute(
            "SELECT COUNT(*) as cnt FROM rate_limits WHERE window_start < ?",
            (_time.time() - 60.0,),
        ).fetchone()["cnt"]
    assert count_after == 0, f"Old rows should be cleaned up, got {count_after}"


def test_flush_unique_index_exists(client):
    """rate_limits must have a UNIQUE index on (key_id, window_start) for UPSERT."""
    from smol_llm_proxy.database import get_db

    with get_db() as conn:
        idx = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_rate_limits_key_window'"
        ).fetchone()

    assert idx is not None, "idx_rate_limits_key_window must exist"
    assert "UNIQUE" in idx["sql"], f"Index must be UNIQUE: {idx['sql']}"


def test_flush_stores_cleared_after_db_write(client):
    """After successful flush, all in-memory store dicts must be cleared."""
    from smol_llm_proxy.auth import create_api_key
    from smol_llm_proxy.rate_limiter import commit_rate, _flush_to_db, _rate_store

    result = create_api_key("flush-clear-test")
    key_id = result["id"]

    commit_rate(key_id, 50)
    assert key_id in _rate_store
    assert len(_rate_store[key_id]) > 0

    asyncio.run(_flush_to_db())

    assert not _rate_store, f"Store should be empty after flush, got: {dict(_rate_store)}"


def test_flush_multiple_keys(client):
    """Flush with multiple different key_ids should write all rows."""
    from smol_llm_proxy.auth import create_api_key
    from smol_llm_proxy.database import get_db
    from smol_llm_proxy.rate_limiter import commit_rate, _flush_to_db

    r1 = create_api_key("flush-multi-1")
    r2 = create_api_key("flush-multi-2")

    commit_rate(r1["id"], 10)
    commit_rate(r2["id"], 20)
    asyncio.run(_flush_to_db())

    with get_db() as conn:
        row1 = conn.execute(
            "SELECT request_count, token_sum FROM rate_limits WHERE key_id = ?",
            (r1["id"],),
        ).fetchone()
        row2 = conn.execute(
            "SELECT request_count, token_sum FROM rate_limits WHERE key_id = ?",
            (r2["id"],),
        ).fetchone()

    assert row1 is not None and row1["request_count"] == 1 and row1["token_sum"] == 10
    assert row2 is not None and row2["request_count"] == 1 and row2["token_sum"] == 20
