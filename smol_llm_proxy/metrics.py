"""Token counting and usage logging."""

import asyncio


_usage_queue: asyncio.Queue = None
_logger_task: asyncio.Task = None


def _init_async_logger():
    global _usage_queue, _logger_task
    if _usage_queue is not None:
        return
    _usage_queue = asyncio.Queue(maxsize=1000)
    _logger_task = asyncio.create_task(_log_worker())


async def _log_worker():
    """Background worker that flushes usage logs to SQLite in batches."""
    from .database import get_db

    batch = []
    while True:
        got_timeout = False
        try:
            item = await asyncio.wait_for(_usage_queue.get(), timeout=1.0)
            batch.append(item)
        except asyncio.TimeoutError:
            got_timeout = True

        should_flush = len(batch) >= 50 or (batch and got_timeout)
        if should_flush:
            _flush_batch(batch)
            batch.clear()


def _flush_batch(batch):
    from .database import get_db
    with get_db() as conn:
        for item in batch:
            try:
                total = item["prompt_tokens"] + item["completion_tokens"]
                conn.execute(
                    """INSERT INTO usage_logs
                       (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens,
                        prompt_ms, predicted_ms)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (item["key_id"], item["server_id"], item["model_name"], item["real_model_name"],
                     item["prompt_tokens"], item["completion_tokens"], total,
                     item["prompt_ms"], item["predicted_ms"]),
                )
            except Exception:
                pass


def enqueue_usage(key_id: int, server_id: int, model_name: str, real_model_name: str,
                  prompt_tokens: int, completion_tokens: int, prompt_ms: float = 0.0, predicted_ms: float = 0.0):
    """Non-blocking usage logging via async queue."""
    if _usage_queue is None:
        _init_async_logger()
    _usage_queue.put_nowait({
        "key_id": key_id,
        "server_id": server_id,
        "model_name": model_name,
        "real_model_name": real_model_name,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "prompt_ms": prompt_ms,
        "predicted_ms": predicted_ms,
    })


async def _shutdown_async_logger():
    """Flush all pending logs and stop the background worker on shutdown."""
    global _logger_task
    if _usage_queue is None:
        return
    # Drain queue and write remaining items
    batch = []
    try:
        while True:
            item = _usage_queue.get_nowait()
            batch.append(item)
    except asyncio.QueueEmpty:
        pass
    if batch:
        _flush_batch(batch)
    # Cancel the worker task
    if _logger_task and not _logger_task.done():
        _logger_task.cancel()
        try:
            await _logger_task
        except asyncio.CancelledError:
            pass


def flush_usage_logs():
    """Synchronously flush any pending usage logs to the database."""
    if _usage_queue is None:
        return
    batch = []
    try:
        while True:
            item = _usage_queue.get_nowait()
            batch.append(item)
    except asyncio.QueueEmpty:
        pass
    if batch:
        _flush_batch(batch)


def log_usage(conn, key_id: int, server_id: int, model_name: str, real_model_name: str,
              prompt_tokens: int, completion_tokens: int, prompt_ms: float = 0.0, predicted_ms: float = 0.0):
    """Write a usage log entry directly to an open connection."""
    total = prompt_tokens + completion_tokens
    conn.execute(
        """INSERT INTO usage_logs
           (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens,
            prompt_ms, predicted_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total,
         prompt_ms, predicted_ms),
    )


def get_usage_logs(key_id=None, server_id=None, start_date=None, end_date=None):
    """Query usage logs with optional filters."""
    clauses = []
    params = []

    if key_id is not None:
        clauses.append("ul.key_id = ?")
        params.append(key_id)
    if server_id is not None:
        clauses.append("ul.server_id = ?")
        params.append(server_id)
    if start_date is not None:
        clauses.append("ul.created_at >= ?")
        params.append(start_date)
    if end_date is not None:
        clauses.append("ul.created_at <= ?")
        params.append(end_date)

    where = ""
    if clauses:
        where = "WHERE " + " AND ".join(clauses)

    query = f"""
        SELECT ul.id, ul.key_id, ul.server_id, ul.model_name, ul.real_model_name,
               ul.prompt_tokens, ul.completion_tokens, ul.total_tokens,
               ul.prompt_ms, ul.predicted_ms,
               ul.created_at, ak.name as user_name, s.name as server_name
        FROM usage_logs ul
        JOIN api_keys ak ON ul.key_id = ak.id
        JOIN servers s ON ul.server_id = s.id
        {where}
        ORDER BY ul.created_at DESC
    """

    from .database import get_db
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_usage_summary(key_id=None, server_id=None):
    """Get aggregated usage stats."""
    clauses = []
    params = []

    if key_id is not None:
        clauses.append("key_id = ?")
        params.append(key_id)
    if server_id is not None:
        clauses.append("server_id = ?")
        params.append(server_id)

    where = ""
    if clauses:
        where = "WHERE " + " AND ".join(clauses)

    query = f"""
        SELECT model_name, real_model_name, COUNT(*) as request_count,
               SUM(prompt_tokens) as total_prompt_tokens,
               SUM(completion_tokens) as total_completion_tokens,
               SUM(total_tokens) as total_all_tokens
        FROM usage_logs
        {where}
        GROUP BY model_name
        ORDER BY total_all_tokens DESC
    """

    from .database import get_db
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
