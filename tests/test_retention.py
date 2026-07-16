"""Tests for usage-log retention cleanup."""

import time


def test_retention_cleanup_deletes_old_rows(client, admin_key):
    """_cleanup_retention must delete rows older than _RETENTION_DAYS days."""
    from smol_llm_proxy.auth import create_api_key
    from smol_llm_proxy.database import get_db
    from smol_llm_proxy.metrics import _cleanup_retention

    result = create_api_key("retention-test")
    key_id = result["id"]

    # Find an existing server_id to satisfy FK constraint
    with get_db() as conn:
        server = conn.execute("SELECT id FROM servers LIMIT 1").fetchone()
        if server:
            server_id = server["id"]
        else:
            # Create a dummy server if none exists
            cur = conn.execute(
                "INSERT INTO servers (name, url, active) VALUES (?, ?, ?)",
                ("retention-server", "http://127.0.0.1:9999", 1),
            )
            server_id = cur.lastrowid

    with get_db() as conn:
        # Insert a row with created_at = 200 days ago
        old_date = time.time() - 200 * 86400 - 10  # extra 10s safety margin
        conn.execute(
            "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, prompt_ms, predicted_ms, created_at) VALUES (?, ?, 'old', 'old', 1, 1, 2, 0, 0, datetime(?, 'unixepoch'))",
            (key_id, server_id, str(old_date)),
        )
        # Insert a fresh row
        conn.execute(
            "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, prompt_ms, predicted_ms, created_at) VALUES (?, ?, 'fresh', 'fresh', 1, 1, 2, 0, 0, datetime('now'))",
            (key_id, server_id),
        )
        conn.commit()

        before = conn.execute("SELECT COUNT(*) as cnt FROM usage_logs").fetchone()["cnt"]
        assert before == 2

    _cleanup_retention()

    with get_db() as conn:
        remaining = conn.execute("SELECT COUNT(*) as cnt FROM usage_logs").fetchone()["cnt"]
        assert remaining == 1, f"Expected 1 row after cleanup, got {remaining}"
        fresh = conn.execute("SELECT model_name FROM usage_logs WHERE model_name = 'fresh'").fetchone()
        assert fresh is not None, "Fresh row should survive cleanup"
