"""Sliding-window rate limiter: admission-time reservation + DB flush."""

import asyncio
import threading
import time as _time

from .database import _get_connection, get_db, get_db_path

_rate_store: dict = {}
_pending: list = []
_lock = asyncio.Lock()
_flush_task: asyncio.Task | None = None
_thread_lock = threading.Lock()
WINDOW_SECONDS = 60
_RATE_LIMITS_SQL = "SELECT COALESCE(SUM(request_count), 0) as rc, COALESCE(SUM(token_sum), 0) as ts FROM rate_limits WHERE key_id = ? AND window_start > ?"


def _flush_to_db_sync(data, pending=None):
    upsert_sql = (
        "INSERT INTO rate_limits (key_id, window_start,"
        " request_count, token_sum)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(key_id, window_start) DO UPDATE SET"
        "  request_count = request_count + excluded.request_count,"
        "  token_sum     = token_sum     + excluded.token_sum"
    )
    try:
        with get_db() as conn:
            for key_id, windows in data.items():
                for ws, vals in windows.items():
                    if vals["rc"] > 0 or vals["ts"] > 0:
                        try:
                            conn.execute(upsert_sql, (key_id, ws, vals["rc"], vals["ts"]))
                        except Exception as e:
                            print(f"rate flush upsert failed (key={key_id}): {e}", flush=True)
            cutoff = _time.time() - WINDOW_SECONDS
            if pending:
                for key_id, ws, rc_delta, ts_delta in pending:
                    if ws > cutoff:
                        try:
                            conn.execute(upsert_sql, (key_id, ws, rc_delta, ts_delta))
                        except Exception as e:
                            print(f"rate flush pending failed (key={key_id}): {e}", flush=True)
            conn.execute(
                "DELETE FROM rate_limits WHERE window_start < ?",
                (_time.time() - 65.0,),
            )
    except Exception as e:
        print(f"rate flush failed: {e}", flush=True)
        return False
    return True


async def _flush_to_db():
    async with _lock:
        with _thread_lock:
            data = {k: dict(v) for k, v in _rate_store.items()}
            pending = list(_pending)
            _rate_store.clear()
            _pending.clear()
        success = await asyncio.to_thread(_flush_to_db_sync, data, pending)
        if not success:
            with _thread_lock:
                for k, v in data.items():
                    store = _rate_store.setdefault(k, {})
                    for ws, vals in v.items():
                        bucket = store.setdefault(ws, {"rc": 0, "ts": 0})
                        bucket["rc"] += vals["rc"]
                        bucket["ts"] += vals["ts"]


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


def reserve_rate(key_id, rpm_limit, tpm_limit, tokens_estimated):
    """Check rate limit and reserve quota under one lock. Returns (allowed, retry_after, ws)."""
    now = _time.time()
    ws = int(now)
    window_start = now - WINDOW_SECONDS
    with _thread_lock:
        store = _rate_store.setdefault(key_id, {})
        bucket = store.setdefault(ws, {"rc": 0, "ts": 0})
        stale = [k for k in store if k <= window_start]
        for k in stale:
            del store[k]
        # SQLite read under _thread_lock serializes the hot path.
        # Acceptable at current scale; defer restructuring until needed.
        db = _get_connection(get_db_path())
        row = db.execute(_RATE_LIMITS_SQL, (key_id, window_start)).fetchone()
        effective_rc = row["rc"] + sum(v["rc"] for v in store.values())
        effective_ts = row["ts"] + sum(v["ts"] for v in store.values())
        if effective_rc + 1 > rpm_limit or effective_ts + tokens_estimated > tpm_limit:
            return False, WINDOW_SECONDS, ws
        bucket["rc"] += 1
        bucket["ts"] += tokens_estimated
        return True, 0, ws


def reconcile_rate(key_id, actual_tokens, admission_bucket, tokens_estimated):
    """Adjust a reservation delta after the upstream responds."""
    delta = actual_tokens - tokens_estimated
    with _thread_lock:
        store = _rate_store.get(key_id)
        if store and admission_bucket in store:
            store[admission_bucket]["ts"] += delta
        else:
            _pending.append((key_id, admission_bucket, 0, delta))
