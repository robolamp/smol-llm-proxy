"""Sliding-window rate limiter: DB read + in-memory commit with UPSERT flush."""

import asyncio
import time as _time

from .database import _get_connection, get_db, get_db_path

_rate_store: dict = {}
_lock = asyncio.Lock()
_flush_task: asyncio.Task | None = None


def _get_store(key_id):
    if key_id not in _rate_store:
        _rate_store[key_id] = {}
    return _rate_store[key_id]


async def _flush_to_db():
    async with _lock:
        if not _rate_store:
            return
        data = {k: dict(v) for k, v in _rate_store.items()}
        store_refs = list(_rate_store.values())
        _rate_store.clear()

    try:
        with get_db() as conn:
            for key_id, windows in data.items():
                for ws, vals in windows.items():
                    if vals["rc"] > 0 or vals["ts"] > 0:
                        conn.execute(
                            "INSERT INTO rate_limits (key_id, window_start,"
                            " request_count, token_sum)"
                            " VALUES (?, ?, ?, ?)"
                            " ON CONFLICT(key_id, window_start) DO UPDATE SET"
                            "  request_count = request_count + excluded.request_count,"
                            "  token_sum     = token_sum     + excluded.token_sum",
                            (key_id, ws, vals["rc"], vals["ts"]),
                        )
            conn.execute(
                "DELETE FROM rate_limits WHERE window_start < ?",
                (_time.time() - 65.0,),
            )
        for windows in store_refs:
            windows.clear()
    except Exception:
        pass


async def _flush_loop():
    while True:
        await asyncio.sleep(1.0)
        await _flush_to_db()


def start_rate_flush():
    global _flush_task
    if _flush_task is None or _flush_task.done():
        _flush_task = asyncio.create_task(_flush_loop())


def stop_rate_flush():
    global _flush_task
    if _flush_task and not _flush_task.done():
        _flush_task.cancel()
        _flush_task = None


def check_rate(key_id, rpm_limit, tpm_limit, tokens_estimated):
    """Read DB baseline + merge in-memory store for per-key sliding window."""
    now = _time.time()
    ws = int(now)
    db = _get_connection(get_db_path())
    row = db.execute(
        "SELECT request_count, token_sum FROM rate_limits WHERE key_id = ? AND window_start = ?",
        (key_id, ws),
    ).fetchone()

    store = _get_store(key_id)
    # Clean expired windows
    for w in [x for x in store if x <= now - 60.0]:
        del store[w]
    # Merge DB data into in-memory store (handles commits that haven't flushed yet)
    db_rc, db_ts = (row["request_count"], row["token_sum"]) if row else (0, 0)
    mem = store.get(ws, {"rc": 0, "ts": 0})
    effective_rc = db_rc + mem["rc"]
    effective_ts = db_ts + mem["ts"]

    if effective_rc + 1 > rpm_limit or effective_ts + tokens_estimated > tpm_limit:
        return False, max(1, 60 - int(now - ws) + 1)
    return True, 0


def commit_rate(key_id, tokens_actual):
    """In-memory increment — flushed to DB by background task."""
    ws = int(_time.time())
    store = _get_store(key_id)
    if ws not in store:
        store[ws] = {"rc": 0, "ts": 0}
    store[ws]["rc"] += 1
    store[ws]["ts"] += tokens_actual
