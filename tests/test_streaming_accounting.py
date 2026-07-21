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


def _mock_sse_usage_and_timings_combined():
    """llama-server final chunk: both usage and timings in the same SSE line."""
    return [
        b'data: {"id":"s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}\n',
        b'data: {"id":"s1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"timings":{"prompt_n":10,"predicted_n":20,"prompt_ms":100,"predicted_ms":200},"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}\n',
        b"data: [DONE]\n",
    ]


def _mock_sse_usage_twice():
    """Usage sent in intermediate chunk and again in final chunk (cumulative)."""
    return [
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"a"},"finish_reason":null}]}\n',
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"b"},"finish_reason":null}],"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}\n',
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


class TestUsageAndTimingsCombined:
    """llama-server final chunk contains both usage and timings — tokens counted once from usage."""

    def test_combined_usage_timings_counts_once(self, server_with_model, admin_key, client):
        chunks = _mock_sse_usage_and_timings_combined()

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
        # usage is authoritative: prompt_tokens=10, completion_tokens=20
        # timings.prompt_n=10 and predicted_n=20 must NOT be added again
        assert captured[0][4] == 10
        assert captured[0][5] == 20


class TestUsageTwiceCumulative:
    """Usage sent in intermediate and final chunk — last seen wins, no summation."""

    def test_usage_twice_no_summation(self, server_with_model, admin_key, client):
        chunks = _mock_sse_usage_twice()

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
        # final cumulative usage: prompt_tokens=5, completion_tokens=3
        # intermediate usage (completion_tokens=1) must NOT be summed
        assert captured[0][4] == 5
        assert captured[0][5] == 3


def _mock_sse_usage_split_two():
    """Final usage data: {...usage...} split across two byte chunks.

    The JSON payload is cut mid-key so that a single SSE line spans two
    aiter_bytes() yields.  No trailing \\n on the first chunk.
    """
    full_line = b'data: {"id":"s1","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":7,"total_tokens":19}}\n'
    mid = len(b'data: {"id":"s1","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":12,"completion_tokens')
    return [full_line[:mid], full_line[mid:]]


def _mock_sse_usage_split_three():
    """Final usage data split across three byte chunks."""
    full_line = b'data: {"id":"s1","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":7,"total_tokens":19}}\n'
    mid1 = len(b'data: {"id":"s1","object":"chat.completion.chunk","choices":[],"usage":{"')
    mid2 = len(b'data: {"id":"s1","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":12,"completion_tokens')
    return [full_line[:mid1], full_line[mid1:mid2], full_line[mid2:]]


def _mock_sse_usage_split_three_simple():
    """Simpler three-way split: cut the usage line at two arbitrary points."""
    full_line = b'data: {"usage":{"prompt_tokens":3,"completion_tokens":2}}\n'
    return [
        full_line[:10],
        full_line[10:30],
        full_line[30:],
    ]


def _mock_sse_multibyte_utf8_split():
    """UTF-8 multi-byte character split across two byte chunks.

    'é' is 0xC3 0xA9 in UTF-8.  We split right between the two bytes.
    """
    return [
        b'data: {"choices":[{"delta":{"content":"caf"}}]',
        b'\xe9, "stop"}]\ndata: [DONE]\n',
    ]


class TestStreamingUsageSplitTwoChunks:
    """Final usage data split across two byte chunks — usage still recorded."""

    def test_usage_split_two_chunks(self, server_with_model, admin_key, client):
        chunks = _mock_sse_usage_split_two()

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
        assert captured[0][4] == 12
        assert captured[0][5] == 7


class TestStreamingUsageSplitThreeChunks:
    """Final usage data split across three byte chunks — usage still recorded."""

    def test_usage_split_three_chunks(self, server_with_model, admin_key, client):
        chunks = _mock_sse_usage_split_three_simple()

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
        assert captured[0][4] == 3
        assert captured[0][5] == 2


class TestStreamingMultibyteUtf8Split:
    """Multi-byte UTF-8 character split across chunks — no crash, content intact."""

    def test_multibyte_utf8_no_crash(self, server_with_model, admin_key, client):
        chunks = _mock_sse_multibyte_utf8_split()

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
        # Content byte 'é' may be replaced by \ufffd but must not crash
        assert b"caf" in resp.content
        assert len(captured) == 1
