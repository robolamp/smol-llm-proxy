"""Token counting and usage logging."""

from datetime import datetime


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
