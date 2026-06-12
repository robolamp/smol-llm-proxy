"""Tests for rate limiting (RPM + TPM)."""

import os
from unittest.mock import patch, AsyncMock


def test_rate_limit_429_on_rpm_exceeded(client, server_with_model):
    """Key hits RPM limit → 429."""
    from smol_llm_proxy.auth import create_api_key
    from smol_llm_proxy.database import get_db

    result = create_api_key("rpm-limit-test")
    user_key = result["key"]

    # Set very low RPM limit
    with get_db() as conn:
        conn.execute(
            "UPDATE api_keys SET rpm_limit = 2, tpm_limit = 1000000 WHERE id = ?",
            (result["id"],),
        )

    mock_body = b'{"id":"c-1","object":"chat.completion","model":"test","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8},"timings":{"prompt_ms":10.0,"predicted_ms":20.0}}'

    with patch("smol_llm_proxy.proxy._forward_request", new=AsyncMock(return_value=(200, mock_body))):
        # First two requests should pass
        r1 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": server_with_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r1.status_code == 200

        r2 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": server_with_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r2.status_code == 200

        # Third request should be rate limited
        r3 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": server_with_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r3.status_code == 429
        assert r3.json()["error"]["type"] == "rate_limit"
        assert "Retry-After" in r3.headers


def test_rate_limit_429_on_tpm_exceeded(client, server_with_model):
    """Key hits TPM limit → 429."""
    from smol_llm_proxy.auth import create_api_key
    from smol_llm_proxy.database import get_db

    result = create_api_key("tpm-limit-test")
    user_key = result["key"]

    # Set very low TPM limit
    with get_db() as conn:
        conn.execute(
            "UPDATE api_keys SET rpm_limit = 10000, tpm_limit = 8 WHERE id = ?",
            (result["id"],),
        )

    mock_body = b'{"id":"c-1","object":"chat.completion","model":"test","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8},"timings":{"prompt_ms":10.0,"predicted_ms":20.0}}'

    with patch("smol_llm_proxy.proxy._forward_request", new=AsyncMock(return_value=(200, mock_body))):
        # First request should pass (est tokens ~4 chars/4 = 1, actual = 8)
        r1 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": server_with_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r1.status_code == 200

        # Second request: estimated tokens (1) + committed (8) = 9 > tpm_limit(8)
        r2 = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": server_with_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r2.status_code == 429


def test_rate_limit_admin_update(client):
    """Admin can update rate limits."""
    from smol_llm_proxy.auth import create_api_key
    from smol_llm_proxy.database import get_db

    result = create_api_key("limit-update-test")

    # Verify default limits
    with get_db() as conn:
        row = conn.execute("SELECT rpm_limit, tpm_limit FROM api_keys WHERE id = ?", (result["id"],)).fetchone()
    assert row["rpm_limit"] == 100
    assert row["tpm_limit"] == 50000

    # Update via admin endpoint
    resp = client.put(
        f"/admin/keys/{result['id']}/limits",
        headers={"Authorization": f"Bearer {os.environ.get('ADMIN_KEY', 'test-key')}"},
        json={"rpm_limit": 5, "tpm_limit": 1000},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["rpm_limit"] == 5
    assert data["tpm_limit"] == 1000

    # Verify in DB
    with get_db() as conn:
        row = conn.execute("SELECT rpm_limit, tpm_limit FROM api_keys WHERE id = ?", (result["id"],)).fetchone()
    assert row["rpm_limit"] == 5
    assert row["tpm_limit"] == 1000


def test_rate_limit_normal_key_passes(client, server_with_model):
    """Normal key with default limits passes through."""
    from smol_llm_proxy.auth import create_api_key

    result = create_api_key("rate-normal-test")
    user_key = result["key"]

    mock_body = b'{"id":"c-1","object":"chat.completion","model":"test","choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8},"timings":{"prompt_ms":10.0,"predicted_ms":20.0}}'

    with patch("smol_llm_proxy.proxy._forward_request", new=AsyncMock(return_value=(200, mock_body))):
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": server_with_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200


def test_rate_limit_list_shows_limits(client):
    """List keys endpoint includes rpm_limit and tpm_limit."""
    from smol_llm_proxy.auth import create_api_key

    create_api_key("rate-list-test")

    resp = client.get(
        "/admin/keys",
        headers={"Authorization": f"Bearer {os.environ.get('ADMIN_KEY', 'test-key')}"},
    )
    assert resp.status_code == 200
    keys = resp.json()
    found = [k for k in keys if k["name"] == "rate-list-test"]
    assert len(found) == 1
    assert "rpm_limit" in found[0]
    assert "tpm_limit" in found[0]
    assert found[0]["rpm_limit"] == 100
    assert found[0]["tpm_limit"] == 50000
