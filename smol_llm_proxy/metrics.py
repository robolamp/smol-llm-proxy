"""Token counting and usage logging."""

import asyncio
import time
from .database import get_db

_usage_queue: asyncio.Queue | None = None
_logger_task: asyncio.Task = None
_retention_task: asyncio.Task = None
_RETENTION_DAYS = 90
_INSERT_SQL = "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, prompt_ms, predicted_ms, key_name, server_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"


def _init_async_logger():
    global _usage_queue, _logger_task
    if _usage_queue is not None:
        return
    _usage_queue = asyncio.Queue(maxsize=1000)
    _logger_task = asyncio.create_task(_log_worker())


async def _log_worker():
    batch = []
    try:
        while True:
            got_timeout = False
            try:
                item = await asyncio.wait_for(_usage_queue.get(), timeout=1.0)
                batch.append(item)
            except asyncio.TimeoutError:
                got_timeout = True
            if got_timeout or len(batch) >= 50:
                try:
                    await _flush_batch(batch)
                except Exception:
                    pass
                batch.clear()
    finally:
        if batch:
            try:
                await _flush_batch(batch)
            except Exception:
                pass


async def _flush_batch(batch):
    await asyncio.to_thread(_flush_batch_sync, batch)


def _flush_batch_sync(batch):
    with get_db() as conn:
        for item in batch:
            try:
                _insert_log(conn, item)
            except Exception:
                pass


def _insert_log(conn, item):
    total = item["prompt_tokens"] + item["completion_tokens"]
    conn.execute(_INSERT_SQL, (
        item["key_id"], item["server_id"], item["model_name"], item["real_model_name"],
        item["prompt_tokens"], item["completion_tokens"], total,
        item["prompt_ms"], item["predicted_ms"],
        item["key_name"], item["server_name"],
    ))


def enqueue_usage(key_id, server_id, model_name, real_model_name, prompt_tokens,
                  completion_tokens, prompt_ms=0.0, predicted_ms=0.0,
                  key_name="", server_name=""):
    if _usage_queue is None:
        _init_async_logger()
    try:
        _usage_queue.put_nowait({
            "key_id": key_id, "server_id": server_id,
            "model_name": model_name, "real_model_name": real_model_name,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "prompt_ms": prompt_ms, "predicted_ms": predicted_ms,
            "key_name": key_name, "server_name": server_name,
        })
    except asyncio.QueueFull:
        print("usage queue full, dropping log entry")


async def _shutdown_async_logger():
    global _logger_task
    if _usage_queue is None:
        return
    batch = _drain_queue()
    if batch:
        await _flush_batch(batch)
    if _logger_task and not _logger_task.done():
        _logger_task.cancel()
        try:
            await _logger_task
        except asyncio.CancelledError:
            pass


def _cleanup_retention():
    cutoff = time.time() - (_RETENTION_DAYS * 86400)
    with get_db() as conn:
        conn.execute("DELETE FROM usage_logs WHERE created_at < datetime(?, 'unixepoch')", (str(cutoff),))


def start_retention_cleanup():
    global _retention_task
    if _retention_task is None or _retention_task.done():
        _retention_task = asyncio.create_task(_retention_loop())


def stop_retention_cleanup():
    global _retention_task
    if _retention_task and not _retention_task.done():
        _retention_task.cancel()
        _retention_task = None


async def _retention_loop():
    while True:
        await asyncio.sleep(86400)
        try:
            await asyncio.to_thread(_cleanup_retention)
        except Exception:
            pass


def _drain_queue():
    batch = []
    try:
        while True:
            batch.append(_usage_queue.get_nowait())
    except asyncio.QueueEmpty:
        pass
    return batch


def _build_where(filters, table_prefix=""):
    prefix = f"{table_prefix}." if table_prefix else ""
    clauses, params = [], []
    for col, db_col, op in (
        ("key_id", "key_id", "="),
        ("server_id", "server_id", "="),
        ("start_date", "created_at", ">="),
        ("end_date", "created_at", "<="),
    ):
        val = filters.get(col)
        if val is not None:
            clauses.append(f"{prefix}{db_col} {op} ?")
            params.append(val)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def _make_filters(key_id=None, server_id=None, start_date=None, end_date=None):
    filters = {}
    for k, v in (("key_id", key_id), ("server_id", server_id), ("start_date", start_date), ("end_date", end_date)):
        if v is not None:
            filters[k] = v
    return filters


def _query_with_filters(query_template, filters, limit=100, offset=0, table_prefix=""):
    where, params = _build_where(filters, table_prefix=table_prefix)
    with get_db() as conn:
        rows = conn.execute(
            query_template.format(where=where) + f" LIMIT {int(limit)} OFFSET {int(offset)}", params
        ).fetchall()
        return [dict(r) for r in rows]


_USAGE_LOGS_SQL = "SELECT ul.id, ul.key_id, ul.server_id, ul.model_name, ul.real_model_name, ul.prompt_tokens, ul.completion_tokens, ul.total_tokens, ul.prompt_ms, ul.predicted_ms, ul.created_at, COALESCE(ak.name, ul.key_name) as user_name, COALESCE(s.name, ul.server_name) as server_name FROM usage_logs ul LEFT JOIN api_keys ak ON ul.key_id = ak.id LEFT JOIN servers s ON ul.server_id = s.id {where} ORDER BY ul.created_at DESC"
_USAGE_SUMMARY_SQL = "SELECT key_name, model_name, real_model_name, COUNT(*) as request_count, SUM(prompt_tokens) as total_prompt_tokens, SUM(completion_tokens) as total_completion_tokens, SUM(total_tokens) as total_all_tokens FROM usage_logs {where} GROUP BY key_name, model_name, real_model_name ORDER BY total_all_tokens DESC"
_USAGE_SUMMARY_REAL_SQL = "SELECT real_model_name, server_id, COUNT(*) as request_count, SUM(prompt_tokens) as total_prompt_tokens, SUM(completion_tokens) as total_completion_tokens, SUM(total_tokens) as total_all_tokens FROM usage_logs {where} GROUP BY real_model_name, server_id ORDER BY total_all_tokens DESC"


def get_usage_logs(key_id=None, server_id=None, start_date=None, end_date=None, limit=100, offset=0):
    return _query_with_filters(
        _USAGE_LOGS_SQL, _make_filters(key_id, server_id, start_date, end_date), limit, offset, table_prefix="ul"
    )


def get_usage_summary(key_id=None, server_id=None, start_date=None, end_date=None):
    return _query_with_filters(_USAGE_SUMMARY_SQL, _make_filters(key_id, server_id, start_date, end_date))


def get_usage_summary_by_real(key_id=None, server_id=None, start_date=None, end_date=None):
    return _query_with_filters(_USAGE_SUMMARY_REAL_SQL, _make_filters(key_id, server_id, start_date, end_date))
