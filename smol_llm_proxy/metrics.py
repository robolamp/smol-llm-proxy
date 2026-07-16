"""Token counting and usage logging."""

import asyncio
from .database import get_db

_usage_queue: asyncio.Queue | None = None
_logger_task: asyncio.Task = None
_INSERT_SQL = "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, prompt_ms, predicted_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"


def _init_async_logger():
    global _usage_queue, _logger_task
    if _usage_queue is not None:
        return
    _usage_queue = asyncio.Queue(maxsize=1000)
    _logger_task = asyncio.create_task(_log_worker())


async def _log_worker():
    batch = []
    while True:
        got_timeout = False
        try:
            item = await asyncio.wait_for(_usage_queue.get(), timeout=1.0)
            batch.append(item)
        except asyncio.TimeoutError:
            got_timeout = True
        if len(batch) >= 50 or (batch and got_timeout):
            _flush_batch(batch)
            batch.clear()


def _flush_batch(batch):
    with get_db() as conn:
        for item in batch:
            try:
                _insert_log(conn, item)
            except Exception as e:
                print(f"Usage log failed: {e}", flush=True)


def _insert_log(conn, item):
    total = item["prompt_tokens"] + item["completion_tokens"]
    conn.execute(
        _INSERT_SQL,
        (
            item["key_id"],
            item["server_id"],
            item["model_name"],
            item["real_model_name"],
            item["prompt_tokens"],
            item["completion_tokens"],
            total,
            item["prompt_ms"],
            item["predicted_ms"],
        ),
    )


def enqueue_usage(
    key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, prompt_ms=0.0, predicted_ms=0.0
):
    if _usage_queue is None:
        _init_async_logger()
    try:
        _usage_queue.put_nowait(
            {
                "key_id": key_id,
                "server_id": server_id,
                "model_name": model_name,
                "real_model_name": real_model_name,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "prompt_ms": prompt_ms,
                "predicted_ms": predicted_ms,
            }
        )
    except asyncio.QueueFull:
        print("usage queue full, dropping log entry", flush=True)


async def _shutdown_async_logger():
    global _logger_task
    if _usage_queue is None:
        return
    batch = _drain_queue()
    if batch:
        _flush_batch(batch)
    if _logger_task and not _logger_task.done():
        _logger_task.cancel()
        try:
            await _logger_task
        except asyncio.CancelledError:
            pass


def flush_usage_logs():
    if _usage_queue is None:
        return
    batch = _drain_queue()
    if batch:
        _flush_batch(batch)


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
    for col, op in [("key_id", "="), ("server_id", "="), ("start_date", ">="), ("end_date", "<=")]:
        val = filters.get(col)
        if val is not None:
            clauses.append(f"{prefix}{col} {op} ?")
            params.append(val)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def _query_with_filters(query_template, filters):
    where, params = _build_where(filters)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(query_template.format(where=where), params).fetchall()]


def get_usage_logs(key_id=None, server_id=None, start_date=None, end_date=None):
    filters = {}
    if key_id is not None:
        filters["key_id"] = key_id
    if server_id is not None:
        filters["server_id"] = server_id
    if start_date is not None:
        filters["start_date"] = start_date
    if end_date is not None:
        filters["end_date"] = end_date
    return _query_with_filters(
        "SELECT ul.id, ul.key_id, ul.server_id, ul.model_name, ul.real_model_name, ul.prompt_tokens, ul.completion_tokens, ul.total_tokens, ul.prompt_ms, ul.predicted_ms, ul.created_at, ak.name as user_name, s.name as server_name FROM usage_logs ul JOIN api_keys ak ON ul.key_id = ak.id JOIN servers s ON ul.server_id = s.id {where} ORDER BY ul.created_at DESC",
        filters,
    )


def get_usage_summary(key_id=None, server_id=None):
    filters = {}
    if key_id is not None:
        filters["key_id"] = key_id
    if server_id is not None:
        filters["server_id"] = server_id
    return _query_with_filters(
        "SELECT model_name, real_model_name, COUNT(*) as request_count, SUM(prompt_tokens) as total_prompt_tokens, SUM(completion_tokens) as total_completion_tokens, SUM(total_tokens) as total_all_tokens FROM usage_logs {where} GROUP BY model_name ORDER BY total_all_tokens DESC",
        filters,
    )
