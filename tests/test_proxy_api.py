"""Tests for proxy endpoints (integration with mock backend)."""

import json
from unittest.mock import patch, AsyncMock


def _mock_response(model_name="model.gguf"):
    return {
        "id": "chatcmpl-123", "object": "chat.completion", "created": 1700000000,
        "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "timings": {"prompt_ms": 12.0, "predicted_ms": 45.0},
    }


class TestProxyAuth:
    def test_no_auth(self, client):
        assert client.post("/v1/chat/completions", json={"model": "m", "messages": []}).status_code == 401

    def test_invalid_key(self, client):
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-invalid"},
            json={"model": "m", "messages": []},
        )
        assert resp.status_code == 403

    def test_inactive_key(self, client, admin_key):
        from smol_llm_proxy.auth import toggle_api_key, list_api_keys
        key_id = [k for k in list_api_keys() if k["name"] == "test-user"][0]["id"]
        toggle_api_key(key_id, False)
        assert client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"model": "m", "messages": []},
        ).status_code == 403

    def test_no_server_for_model(self, client, admin_key):
        assert client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"model": "unknown.gguf", "messages": []},
        ).status_code == 404


class TestProxyForwarding:
    def test_successful_proxy(self, server_with_model, admin_key, client):
        mock_data = _mock_response(server_with_model["model_name"])
        with patch(
            "smol_llm_proxy.proxy._forward_request",
            new=AsyncMock(return_value=(200, json.dumps(mock_data).encode())),
        ):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={"model": server_with_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
            )
        assert resp.status_code == 200

    def test_502_on_connect_error(self, server_with_model, admin_key, client):
        from httpx import ConnectError
        with patch(
            "smol_llm_proxy.proxy._forward_request",
            new=AsyncMock(side_effect=ConnectError("refused")),
        ):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={"model": server_with_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
            )
        assert resp.status_code == 502


class TestUsageLogging:
    def test_tokens_and_timings_logged(self, server_with_model, admin_key, client):
        mock_data = _mock_response(server_with_model["model_name"])
        with patch(
            "smol_llm_proxy.proxy._forward_request",
            new=AsyncMock(return_value=(200, json.dumps(mock_data).encode())),
        ):
            client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={"model": server_with_model["model_name"], "messages": [{"role": "user", "content": "hi"}]},
            )

        from smol_llm_proxy.metrics import flush_usage_logs, get_usage_logs
        flush_usage_logs()
        logs = get_usage_logs()
        last = logs[0]
        assert last["prompt_tokens"] == 10
        assert last["completion_tokens"] == 5
        assert last["total_tokens"] == 15
        assert last["prompt_ms"] == 12.0
        assert last["predicted_ms"] == 45.0


class TestAliasResolution:
    def test_alias_routed_and_logged(self, server_with_model, admin_key, client):
        from smol_llm_proxy.database import get_db
        alias = f"my-alias-{id(client)}"
        with get_db() as conn:
            conn.execute(
                "INSERT INTO model_aliases (alias_name, real_model_name) VALUES (?, ?)",
                (alias, server_with_model["model_name"]),
            )

        mock_data = _mock_response(server_with_model["model_name"])
        with patch(
            "smol_llm_proxy.proxy._forward_request",
            new=AsyncMock(return_value=(200, json.dumps(mock_data).encode())),
        ):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={"model": alias, "messages": [{"role": "user", "content": "hi"}]},
            )

        assert resp.status_code == 200

        from smol_llm_proxy.metrics import flush_usage_logs, get_usage_logs
        flush_usage_logs()
        last = get_usage_logs()[0]
        assert last["model_name"] == alias
        assert last["real_model_name"] == server_with_model["model_name"]


class TestProxyHelpers:
    def test_extract_key(self):
        from smol_llm_proxy.proxy import _extract_user_key
        assert _extract_user_key("Bearer my-key") == "my-key"
        assert _extract_user_key(None) is None

    def test_format_url(self):
        from smol_llm_proxy.proxy import _format_server_url
        assert _format_server_url("http://host:8080/", "/v1/chat") == "http://host:8080/v1/chat"

    def test_parse_usage_from_body(self):
        from smol_llm_proxy.proxy import _parse_usage_from_body
        pt, ct, pm, pr = _parse_usage_from_body(
            b'{"usage":{"prompt_tokens":5,"completion_tokens":10},"timings":{"prompt_ms":12.3,"predicted_ms":45.6}}'
        )
        assert pt == 5 and ct == 10 and pm == 12.3 and pr == 45.6

    def test_parse_sse_llama_cpp(self):
        from smol_llm_proxy.proxy import _parse_sse_usage
        pt, ct, pm, pr = _parse_sse_usage(
            'data: {"timings":{"prompt_n":13,"predicted_n":10,"prompt_ms":16.8,"predicted_ms":57.2}}'
        )
        assert pt == 13 and ct == 10

    def test_parse_sse_done(self):
        from smol_llm_proxy.proxy import _parse_sse_usage
        assert _parse_sse_usage("[DONE]") == (0, 0, 0.0, 0.0)
