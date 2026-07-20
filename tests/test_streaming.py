"""Tests for streaming proxy endpoint."""

from unittest.mock import patch, Mock, AsyncMock


def _mock_sse_chunks():
    return [
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}\n',
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}\n',
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n',
        b"data: [DONE]\n",
    ]


def _mock_sse_with_usage():
    return [
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n',
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n',
        b"data: [DONE]\n",
    ]


class TestStreamingProxy:
    def test_streaming_full_flow(self, server_with_model, admin_key, client):
        """Streaming request through proxy with mocked upstream SSE."""
        chunks = _mock_sse_chunks()

        async def mock_aiter_bytes():
            for chunk in chunks:
                yield chunk

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.aiter_bytes = mock_aiter_bytes
        mock_response.aread = AsyncMock(return_value=b"")
        mock_response.aclose = AsyncMock()

        mock_client = Mock()
        mock_client.build_request = Mock(return_value=Mock())
        mock_client.send = AsyncMock(return_value=mock_response)

        with patch(
            "smol_llm_proxy.proxy.get_httpx_client",
            new=Mock(return_value=mock_client),
        ):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={
                    "model": server_with_model["model_name"],
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        assert resp.status_code == 200
        content = resp.content.decode()
        assert "data:" in content
        assert "[DONE]" in content

    def test_streaming_usage_logged(self, server_with_model, admin_key, client):
        """Streaming request logs tokens from SSE usage line."""
        chunks = _mock_sse_with_usage()

        async def mock_aiter_bytes():
            for chunk in chunks:
                yield chunk

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.aiter_bytes = mock_aiter_bytes
        mock_response.aread = AsyncMock(return_value=b"")
        mock_response.aclose = AsyncMock()

        mock_client = Mock()
        mock_client.build_request = Mock(return_value=Mock())
        mock_client.send = AsyncMock(return_value=mock_response)

        with patch(
            "smol_llm_proxy.proxy.get_httpx_client",
            new=Mock(return_value=mock_client),
        ):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={
                    "model": server_with_model["model_name"],
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        assert resp.status_code == 200

        import smol_llm_proxy.metrics as _m

        if _m._usage_queue is not None:
            batch = _m._drain_queue()
            if batch:
                _m._flush_batch_sync(batch)
        from smol_llm_proxy.metrics import get_usage_logs

        logs = get_usage_logs()
        last = logs[0]
        assert last["prompt_tokens"] == 5
        assert last["completion_tokens"] == 3

    def test_streaming_upstream_error_returns_real_status(self, server_with_model, admin_key, client):
        """Non-200 upstream status returns plain Response with real status code."""
        error_body = b'{"error":{"message":"internal server error","type":"server_error"}}'

        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.aread = AsyncMock(return_value=error_body)
        mock_response.aclose = AsyncMock()

        mock_client = Mock()
        mock_client.build_request = Mock(return_value=Mock())
        mock_client.send = AsyncMock(return_value=mock_response)

        with patch(
            "smol_llm_proxy.proxy.get_httpx_client",
            new=Mock(return_value=mock_client),
        ):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={
                    "model": server_with_model["model_name"],
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        assert resp.status_code == 500
        assert error_body in resp.content

    def test_streaming_connect_error_returns_502(self, server_with_model, admin_key, client):
        """httpx.ConnectError in streaming path must return 502, not raw 500."""
        from httpx import ConnectError

        mock_client = Mock()
        mock_client.build_request = Mock(return_value=Mock())
        mock_client.send = AsyncMock(side_effect=ConnectError("refused"))

        with patch(
            "smol_llm_proxy.proxy.get_httpx_client",
            new=Mock(return_value=mock_client),
        ):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={
                    "model": server_with_model["model_name"],
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        assert resp.status_code == 502

    def test_streaming_timeout_returns_504(self, server_with_model, admin_key, client):
        """httpx.TimeoutError in streaming path must return 504."""
        from httpx import TimeoutException

        mock_client = Mock()
        mock_client.build_request = Mock(return_value=Mock())
        mock_client.send = AsyncMock(side_effect=TimeoutException("timeout"))

        with patch(
            "smol_llm_proxy.proxy.get_httpx_client",
            new=Mock(return_value=mock_client),
        ):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={
                    "model": server_with_model["model_name"],
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        assert resp.status_code == 504

    def test_streaming_upstream_error_has_content_type(self, server_with_model, admin_key, client):
        """Non-200 upstream streaming error returns application/json content-type."""
        error_body = b'{"error":{"message":"internal server error","type":"server_error"}}'

        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.aread = AsyncMock(return_value=error_body)
        mock_response.aclose = AsyncMock()

        mock_client = Mock()
        mock_client.build_request = Mock(return_value=Mock())
        mock_client.send = AsyncMock(return_value=mock_response)

        with patch(
            "smol_llm_proxy.proxy.get_httpx_client",
            new=Mock(return_value=mock_client),
        ):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={
                    "model": server_with_model["model_name"],
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        assert resp.status_code == 500
        assert "application/json" in resp.headers.get("content-type", "")
