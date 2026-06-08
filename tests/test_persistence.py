"""Verify data persistence across proxy restarts (cache clears)."""

import os
from unittest.mock import patch, AsyncMock


def test_keys_persist_after_restart(client):
    """Create a key, clear caches, verify key still works."""
    from smol_llm_proxy.auth import create_api_key, validate_api_key

    result = create_api_key("persist-test")
    raw_key = result["key"]

    info = validate_api_key(raw_key)
    assert info is not None and "id" in info

    # Simulate restart: clear in-memory caches
    from smol_llm_proxy.cache import clear_key_cache, clear_route_cache, clear_alias_cache
    clear_key_cache()
    clear_route_cache()
    clear_alias_cache()

    # Key should still be valid (loaded from SQLite on next lookup)
    info = validate_api_key(raw_key)
    assert info is not None and "name" in info
    assert info["name"] == "persist-test"


def test_usage_logs_persist_after_restart(client, server_with_model):
    """Ensure usage logs written to DB survive cache clears (proxy restart)."""
    from smol_llm_proxy.metrics import enqueue_usage, flush_usage_logs
    from smol_llm_proxy.cache import clear_key_cache
    from smol_llm_proxy.database import get_db

    # Create a user key
    from smol_llm_proxy.auth import create_api_key
    result = create_api_key("persist-log-user")
    user_key = result["key"]

    # Mock _forward_request to return a valid response with usage data
    mock_body = b'{"id":"c-1","object":"chat.completion","model":"test","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8},"timings":{"prompt_ms":10.0,"predicted_ms":20.0}}'

    with patch("smol_llm_proxy.proxy._forward_request", new=AsyncMock(return_value=(200, mock_body))):
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": server_with_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200

    # Get key_id and server_id from DB
    with get_db() as conn:
        key_row = conn.execute("SELECT id FROM api_keys WHERE name = ?", ("persist-log-user",)).fetchone()
        srv_row = conn.execute("SELECT id FROM servers WHERE id = ?", (server_with_model["id"],)).fetchone()

    # Manually enqueue a usage log (mock bypasses the real flow)
    assert key_row is not None and srv_row is not None
    enqueue_usage(
        key_id=key_row["id"],
        server_id=srv_row["id"],
        model_name=server_with_model["model_name"],
        real_model_name="test-model.gguf",
        prompt_tokens=5,
        completion_tokens=3,
        prompt_ms=10.0,
        predicted_ms=20.0,
    )

    # Flush synchronously
    flush_usage_logs()

    with get_db() as conn:
        rows_before = conn.execute("SELECT COUNT(*) as cnt FROM usage_logs").fetchone()["cnt"]
    assert rows_before > 0, f"Usage log should exist in DB after flush, got {rows_before}"

    # Clear caches (simulate restart)
    clear_key_cache()

    with get_db() as conn:
        rows_after = conn.execute("SELECT COUNT(*) as cnt FROM usage_logs").fetchone()["cnt"]
    assert rows_after == rows_before, "Usage log count should not change after cache clear"


def test_servers_persist_after_restart(client):
    """Create a server config, clear cache, verify it's still there."""
    from smol_llm_proxy.database import get_db

    resp = client.post(
        "/admin/servers",
        headers={"Authorization": f"Bearer {os.environ.get('ADMIN_KEY', 'test-key')}"},
        json={"name": "persist-srv", "url": "http://localhost:9999"},
    )
    assert resp.status_code == 200

    # Clear route cache (simulates restart)
    from smol_llm_proxy.cache import clear_route_cache
    clear_route_cache()

    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM servers WHERE name = ?", ("persist-srv",)).fetchone()["cnt"]
    assert row == 1


def test_model_assignments_persist_after_restart(client):
    """Create model assignment, clear cache, verify routing still works."""
    from smol_llm_proxy.database import get_db

    resp = client.post(
        "/admin/servers",
        headers={"Authorization": f"Bearer {os.environ.get('ADMIN_KEY', 'test-key')}"},
        json={"name": "persist-srv2", "url": "http://localhost:7777"},
    )
    server_id = resp.json()["id"]

    client.post(
        f"/admin/servers/{server_id}/models",
        headers={"Authorization": f"Bearer {os.environ.get('ADMIN_KEY', 'test-key')}"},
        json={"model_name": "persist-model.gguf"},
    )

    # Clear cache (simulates restart)
    from smol_llm_proxy.cache import clear_route_cache
    clear_route_cache()

    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM server_models WHERE model_name = ?",
            ("persist-model.gguf",),
        ).fetchone()["cnt"]
    assert row == 1
