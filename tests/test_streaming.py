"""Tests for streaming proxy endpoint."""

from unittest.mock import patch


def _mock_sse_chunks():
    return [
        'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}\n',
        'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" world"},"finish_reason":null}]}\n',
        'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n',
        "data: [DONE]\n",
    ]


def _mock_sse_with_usage():
    return [
        'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n',
        'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n',
        "data: [DONE]\n",
    ]


class TestStreamingProxy:
    def test_streaming_full_flow(self, server_with_model, admin_key, client):
        """Streaming request through proxy with mocked upstream SSE."""
        chunks = _mock_sse_chunks()

        async def mock_gen(*args, **kwargs):
            for chunk in chunks:
                yield chunk

        with patch("smol_llm_proxy.proxy._stream_chunks", new=mock_gen):
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

        async def mock_gen(*args, **kwargs):
            for chunk in chunks:
                yield chunk

        with patch("smol_llm_proxy.proxy._stream_chunks", new=mock_gen):
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

        from smol_llm_proxy.metrics import flush_usage_logs, get_usage_logs

        flush_usage_logs()
        logs = get_usage_logs()
        last = logs[0]
        assert last["prompt_tokens"] == 5
        assert last["completion_tokens"] == 3
