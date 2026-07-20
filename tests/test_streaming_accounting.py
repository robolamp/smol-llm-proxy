"""Streaming accounting: delta-event fallback when no timings/usage arrives."""

from unittest.mock import patch, Mock, AsyncMock

from smol_llm_proxy.proxy import _parse_sse_chunk


def _mock_sse_no_usage(n_tokens=5):
    """N delta events, no timings, no usage — the real llama.cpp tail."""
    chunks = []
    for i in range(n_tokens):
        chunks.append(
            b'data: {"id":"s1","object":"chat.completion.chunk",'
            b'"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}\n'
        )
    chunks.append(b"data: [DONE]\n")
    return chunks


def _mock_sse_abort(n_total=5, abort_at=2):
    """Client disconnects after abort_at events."""
    chunks = []
    for i in range(n_total):
        chunks.append(
            b'data: {"id":"s1","object":"chat.completion.chunk",'
            b'"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}\n'
        )
    if abort_at < n_total:
        return chunks[:abort_at]
    chunks.append(b"data: [DONE]\n")
    return chunks


def _mock_sse_split_timings():
    """Final timings event split across two byte chunks."""
    chunk1 = (
        b'data: {"id":"s1","object":"chat.completion.chunk",'
        b'"choices":[{"index":0,"delta":{"content":"ab"},"finish_reason":null}]}'
    )
    chunk2 = b'\ndata: {"timings":{"prompt_n":5,"predicted_n":7,"prompt_ms":100,"predicted_ms":200}}\ndata: [DONE]\n'
    return [chunk1, chunk2]


def _mock_sse_with_usage():
    """SSE with standard OpenAI usage line."""
    return [
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n',
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n',
        b"data: [DONE]\n",
    ]


class TestParseSseChunk:
    def test_counts_delta_content(self):
        text = (
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n'
            'data: {"choices":[{"delta":{"content":""}}]}\n'
            "data: [DONE]\n"
        )
        _, _, dc, _, _ = _parse_sse_chunk(text)
        assert dc == 5

    def test_ignores_empty_content(self):
        text = 'data: {"choices":[{"delta":{}}]}\n'
        _, _, dc, _, _ = _parse_sse_chunk(text)
        assert dc == 0

    def test_ignores_no_choices(self):
        text = 'data: {"timings":{"prompt_n":1,"predicted_n":2}}\n'
        _, _, dc, _, _ = _parse_sse_chunk(text)
        assert dc == 0


class TestStreamingNoUsage:
    """When upstream sends no timings/usage, delta events provide billing."""

    def test_no_usage_billed_from_deltas(self, server_with_model, admin_key, client):
        n = 5
        chunks = _mock_sse_no_usage(n)

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

        captured = []

        def capture_enqueue(*args, **kwargs):
            captured.append(args)

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client)):
            with patch("smol_llm_proxy.proxy.enqueue_usage", side_effect=capture_enqueue):
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
        assert len(captured) == 1
        # args: key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, ...
        prompt_tokens = captured[0][4]
        completion_tokens = captured[0][5]
        assert completion_tokens == n
        assert prompt_tokens > 0  # from estimate


class TestStreamingAbort:
    """Client disconnects mid-stream: billed for what was streamed, not refunded."""

    def test_abort_billed_not_refunded(self, server_with_model, admin_key, client):
        chunks = _mock_sse_abort(n_total=5, abort_at=2)

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

        captured = []

        def capture_enqueue(*args, **kwargs):
            captured.append(args)

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client)):
            with patch("smol_llm_proxy.proxy.enqueue_usage", side_effect=capture_enqueue):
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
        assert len(captured) == 1
        completion_tokens = captured[0][5]
        # 2 tokens streamed before abort
        assert completion_tokens == 2
        assert captured[0][4] > 0  # prompt from estimate


class TestStreamingSplit:
    """Final timings event split across two byte chunks — accounting survives."""

    def test_split_timings_survives(self, server_with_model, admin_key, client):
        chunks = _mock_sse_split_timings()

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

        captured = []

        def capture_enqueue(*args, **kwargs):
            captured.append(args)

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client)):
            with patch("smol_llm_proxy.proxy.enqueue_usage", side_effect=capture_enqueue):
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
        assert len(captured) == 1
        # timings carries predicted_n=7
        assert captured[0][5] == 7
        assert captured[0][4] == 5


class TestStreamingWithUsageStillWorks:
    """Ensure the fix doesn't break the existing usage-from-SSE path."""

    def test_usage_from_sse_still_works(self, server_with_model, admin_key, client):
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

        captured = []

        def capture_enqueue(*args, **kwargs):
            captured.append(args)

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client)):
            with patch("smol_llm_proxy.proxy.enqueue_usage", side_effect=capture_enqueue):
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
        assert len(captured) == 1
        # authoritative usage: prompt_tokens=5, completion_tokens=3
        assert captured[0][4] == 5
        assert captured[0][5] == 3
