"""Migration of rate_limits index from non-UNIQUE to UNIQUE on dirty DB."""

import os
import sqlite3


def _make_conn(db_file):
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    return conn


def test_migrate_dirty_db_with_duplicates(tmp_path):
    """Simulate an old DB with duplicate (key_id, window_start) rows and a non-unique index.
    init_db() should clear duplicates, rebuild the UNIQUE index, and leave the table intact."""
    db_file = tmp_path / "dirty.db"

    # Build a dirty DB: non-UNIQUE index + duplicate rows (the old state)
    conn = _make_conn(db_file)
    conn.execute("""CREATE TABLE rate_limits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key_id INTEGER NOT NULL,
        window_start REAL NOT NULL,
        request_count INTEGER NOT NULL DEFAULT 0,
        token_sum INTEGER NOT NULL DEFAULT 0
    )""")
    # Non-UNIQUE index (old schema)
    conn.execute("CREATE INDEX idx_rate_limits_key_window ON rate_limits(key_id, window_start)")
    # Duplicate rows that would break CREATE UNIQUE INDEX
    conn.execute("INSERT INTO rate_limits (key_id, window_start, request_count, token_sum) VALUES (1, 1000.0, 5, 200)")
    conn.execute("INSERT INTO rate_limits (key_id, window_start, request_count, token_sum) VALUES (1, 1000.0, 3, 100)")
    conn.commit()
    conn.close()

    # Point the proxy at this dirty DB and call init_db (migration path)
    os.environ["DB_PATH"] = str(db_file)

    # Reset thread-local connection so init_db opens a fresh handle
    from smol_llm_proxy.database import _thread_local

    if hasattr(_thread_local, "conn") and _thread_local.conn is not None:
        _thread_local.conn.close()
    _thread_local.conn = None

    from smol_llm_proxy.database import init_db

    init_db()

    # Verify: UNIQUE index exists
    conn = _make_conn(db_file)
    idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_rate_limits_key_window'"
    ).fetchone()
    assert idx is not None, "index must exist after migration"
    assert "UNIQUE" in idx["sql"], f"index must be UNIQUE: {idx['sql']}"

    # Verify: table is cleared (ephemeral data — migration deletes all rows before rebuild)
    rows = conn.execute("SELECT COUNT(*) as cnt FROM rate_limits").fetchone()
    assert rows["cnt"] == 0, f"Expected 0 rows after migration cleanup, got {rows['cnt']}"

    conn.close()


def test_migrate_already_unique_db_is_noop(tmp_path):
    """If the UNIQUE index already exists, init_db must not touch the table."""
    db_file = tmp_path / "clean.db"

    conn = _make_conn(db_file)
    conn.execute("""CREATE TABLE rate_limits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key_id INTEGER NOT NULL,
        window_start REAL NOT NULL,
        request_count INTEGER NOT NULL DEFAULT 0,
        token_sum INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("CREATE UNIQUE INDEX idx_rate_limits_key_window ON rate_limits(key_id, window_start)")
    conn.execute("INSERT INTO rate_limits (key_id, window_start, request_count, token_sum) VALUES (42, 5000.0, 7, 350)")
    conn.commit()
    conn.close()

    os.environ["DB_PATH"] = str(db_file)

    from smol_llm_proxy.database import _thread_local

    if hasattr(_thread_local, "conn") and _thread_local.conn is not None:
        _thread_local.conn.close()
    _thread_local.conn = None

    from smol_llm_proxy.database import init_db

    init_db()

    conn = _make_conn(db_file)
    rows = conn.execute("SELECT key_id, window_start, request_count, token_sum FROM rate_limits").fetchall()
    assert len(rows) == 1
    assert rows[0]["key_id"] == 42
    assert rows[0]["request_count"] == 7
    conn.close()
