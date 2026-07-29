"""Tests for proxy endpoints (integration with mock backend)."""

import json
from unittest.mock import AsyncMock, Mock, patch


def _mock_response(model_name="model.gguf"):
    return {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "created": 1700000000,
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
        from smol_llm_proxy.auth import list_api_keys, toggle_api_key

        key_id = [k for k in list_api_keys() if k["name"] == "test-user"][0]["id"]
        toggle_api_key(key_id, False)
        assert (
            client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={"model": "m", "messages": []},
            ).status_code
            == 403
        )

    def test_no_server_for_model(self, client, admin_key):
        assert (
            client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={"model": "unknown.gguf", "messages": []},
            ).status_code
            == 404
        )


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

        mock_client = Mock()
        mock_client.request = AsyncMock(side_effect=ConnectError("refused"))

        with patch(
            "smol_llm_proxy.proxy.get_httpx_client",
            new=Mock(return_value=mock_client),
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

        import smol_llm_proxy.metrics as _m

        if _m._usage_queue is not None:
            batch = _m._drain_queue()
            if batch:
                _m._flush_batch_sync(batch)
        from smol_llm_proxy.metrics import get_usage_logs

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

        import smol_llm_proxy.metrics as _m

        if _m._usage_queue is not None:
            batch = _m._drain_queue()
            if batch:
                _m._flush_batch_sync(batch)
        from smol_llm_proxy.metrics import get_usage_logs

        last = get_usage_logs()[0]
        assert last["model_name"] == alias
        assert last["real_model_name"] == server_with_model["model_name"]


class TestForwardRequestMethod:
    def test_forward_request_uses_correct_method(self):
        """_forward_request must pass the method parameter to httpx, not hardcode POST."""
        import asyncio

        mock_client = Mock()
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"data":[]}'
        mock_client.request = AsyncMock(return_value=mock_resp)

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client)):
            from smol_llm_proxy.proxy import _forward_request

            asyncio.run(_forward_request("http://localhost:8080/v1/models", {}, b"", "GET"))

        mock_client.request.assert_called_once()
        call_kwargs = mock_client.request.call_args
        assert call_kwargs.kwargs["method"] == "GET"

    def test_forward_request_post_method(self):
        """_forward_request passes POST method correctly."""
        import asyncio

        mock_client = Mock()
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"id":"test"}'
        mock_client.request = AsyncMock(return_value=mock_resp)

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client)):
            from smol_llm_proxy.proxy import _forward_request

            asyncio.run(_forward_request("http://localhost:8080/v1/chat", {}, b'{"model":"m"}', "POST"))

        mock_client.request.assert_called_once()
        call_kwargs = mock_client.request.call_args
        assert call_kwargs.kwargs["method"] == "POST"


class TestModelsEndpoint:
    def test_models_no_auth(self, client):
        resp = client.get("/v1/models")
        assert resp.status_code == 401

    def test_models_invalid_key(self, client):
        resp = client.get("/v1/models", headers={"Authorization": "Bearer sk-invalid"})
        assert resp.status_code == 403

    def test_models_inactive_key(self, client, admin_key):
        from smol_llm_proxy.auth import list_api_keys, toggle_api_key

        key_id = [k for k in list_api_keys() if k["name"] == "test-user"][0]["id"]
        toggle_api_key(key_id, False)
        resp = client.get("/v1/models", headers={"Authorization": f"Bearer {admin_key}"})
        assert resp.status_code == 403

    def test_models_valid_key(self, server_with_model, admin_key, client):
        mock_data = {"object": "list", "data": [{"id": server_with_model["model_name"], "object": "model"}]}
        with patch(
            "smol_llm_proxy.proxy._forward_request",
            new=AsyncMock(return_value=(200, json.dumps(mock_data).encode())),
        ):
            resp = client.get("/v1/models", headers={"Authorization": f"Bearer {admin_key}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        assert isinstance(data["data"], list)


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
        from smol_llm_proxy.proxy import _parse_sse_chunk

        pt, ct, dc, pm, pr = _parse_sse_chunk(
            'data: {"timings":{"prompt_n":13,"predicted_n":10,"prompt_ms":16.8,"predicted_ms":57.2}}'
        )
        assert pt == 13 and ct == 10 and dc == 0

    def test_parse_sse_done(self):
        from smol_llm_proxy.proxy import _parse_sse_chunk

        assert _parse_sse_chunk("[DONE]") == (0, 0, 0, 0.0, 0.0)
