"""Tests for SSE keepalive behaviour in proxy_streaming."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from smol_llm_proxy.proxy import _keepalive_stream


class TestStreamingKeepalive:
    """Integration tests for SSE keepalive while upstream is slow."""

    def test_slow_upstream_headers_delayed(self, server_with_model, admin_key, client):
        """Slow upstream (> interval): status 200, text/event-stream, keepalives + real SSE payload."""
        chunks = [
            b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}\n',
            b"data: [DONE]\n",
        ]

        async def mock_aiter_bytes():
            for chunk in chunks:
                yield chunk

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.aiter_bytes = mock_aiter_bytes
        mock_response.aread = AsyncMock(return_value=b"")
        mock_response.aclose = AsyncMock()

        async def slow_send(request, **kwargs):
            await asyncio.sleep(0.15)
            return mock_response

        mock_client = Mock()
        mock_client.build_request = Mock(return_value=Mock())
        mock_client.send = slow_send

        captured = []

        def capture_enqueue(*args, **kwargs):
            captured.append(args)

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client)):
            with patch("smol_llm_proxy.proxy.SSE_KEEPALIVE_INTERVAL", 0.02):
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
        assert "text/event-stream" in resp.headers.get("content-type", "")
        content = resp.content
        assert content.startswith(b": keepalive\n\n")
        assert b": keepalive" in content
        assert b'"content":"hi"' in content
        assert b"[DONE]" in content
        assert len(captured) == 1
        assert captured[0][4] > 0  # prompt tokens from the request-body estimate
        assert captured[0][5] == 2  # completion: 2 streamed delta characters ("hi")

    def test_httpx_connect_error_after_keepalives_started(self, server_with_model, admin_key, client):
        """ConnectError after keepalives: status 200, contains keepalive + error event, no [DONE]."""
        from httpx import ConnectError

        enqueue_mock = Mock()
        reconcile_mock = Mock()

        async def slow_send(request, **kwargs):
            await asyncio.sleep(0.15)
            raise ConnectError("refused")

        mock_client = Mock()
        mock_client.build_request = Mock(return_value=Mock())
        mock_client.send = slow_send

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client)):
            with patch("smol_llm_proxy.proxy.SSE_KEEPALIVE_INTERVAL", 0.02):
                with patch("smol_llm_proxy.proxy.enqueue_usage", enqueue_mock):
                    with patch("smol_llm_proxy.proxy.reconcile_rate", reconcile_mock):
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
        content = resp.content
        assert b": keepalive" in content
        assert b'"type":"upstream_error"' in content
        assert b'"code":502' in content
        assert b"[DONE]" not in content
        enqueue_mock.assert_not_called()
        reconcile_mock.assert_not_called()

    def test_non_200_upstream_after_keepalives_started(self, server_with_model, admin_key, client):
        """Non-200 upstream after keepalives: body contains error message and code."""
        error_body = b'{"error":{"message":"boom"}}'

        enqueue_mock = Mock()
        reconcile_mock = Mock()

        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.aread = AsyncMock(return_value=error_body)
        mock_response.aclose = AsyncMock()

        async def slow_send(request, **kwargs):
            await asyncio.sleep(0.15)
            return mock_response

        mock_client = Mock()
        mock_client.build_request = Mock(return_value=Mock())
        mock_client.send = slow_send

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client)):
            with patch("smol_llm_proxy.proxy.SSE_KEEPALIVE_INTERVAL", 0.02):
                with patch("smol_llm_proxy.proxy.enqueue_usage", enqueue_mock):
                    with patch("smol_llm_proxy.proxy.reconcile_rate", reconcile_mock):
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
        content = resp.content
        assert b'"message":"boom"' in content
        assert b'"code":500' in content
        enqueue_mock.assert_not_called()
        reconcile_mock.assert_not_called()

    def test_fast_errors_still_return_real_status_codes(self, server_with_model, admin_key, client):
        """Fast errors (no keepalive delay) still return 502/504 and body has no : keepalive."""
        from httpx import ConnectError, TimeoutException

        # ConnectError → 502
        mock_client = Mock()
        mock_client.build_request = Mock(return_value=Mock())
        mock_client.send = AsyncMock(side_effect=ConnectError("refused"))

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client)):
            with patch("smol_llm_proxy.proxy.SSE_KEEPALIVE_INTERVAL", 0.02):
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
        assert b": keepalive" not in resp.content

        # TimeoutException → 504
        mock_client2 = Mock()
        mock_client2.build_request = Mock(return_value=Mock())
        mock_client2.send = AsyncMock(side_effect=TimeoutException("timeout"))

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client2)):
            with patch("smol_llm_proxy.proxy.SSE_KEEPALIVE_INTERVAL", 0.02):
                resp2 = client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {admin_key}"},
                    json={
                        "model": server_with_model["model_name"],
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                )

        assert resp2.status_code == 504
        assert b": keepalive" not in resp2.content

    def test_sse_keepalive_interval_0_disables_keepalive(self, server_with_model, admin_key, client):
        """SSE_KEEPALIVE_INTERVAL=0 disables keepalive: slow upstream still works, no : keepalive."""
        chunks = [
            b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n',
            b"data: [DONE]\n",
        ]

        async def mock_aiter_bytes():
            for chunk in chunks:
                yield chunk

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.aiter_bytes = mock_aiter_bytes
        mock_response.aread = AsyncMock(return_value=b"")
        mock_response.aclose = AsyncMock()

        async def slow_send(request, **kwargs):
            await asyncio.sleep(0.2)
            return mock_response

        mock_client = Mock()
        mock_client.build_request = Mock(return_value=Mock())
        mock_client.send = slow_send

        with patch("smol_llm_proxy.proxy.get_httpx_client", new=Mock(return_value=mock_client)):
            with patch("smol_llm_proxy.proxy.SSE_KEEPALIVE_INTERVAL", 0):
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
        content = resp.content
        assert b"[DONE]" in content
        assert b": keepalive" not in content


def test_client_disconnect_while_waiting_cancels_send():
    """The client goes away while the upstream send is still pending → the send task is cancelled."""
    outcome = {"cancelled": False}

    async def slow_send():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            outcome["cancelled"] = True
            raise

    async def run():
        with patch("smol_llm_proxy.proxy.SSE_KEEPALIVE_INTERVAL", 0.02):
            task = asyncio.create_task(slow_send())
            agen = _keepalive_stream(task, {}, {}, "", "", 0, 0)
            assert await agen.__anext__() == b": keepalive\n\n"
            await agen.aclose()
            assert task.cancelled()

    asyncio.run(run())
    assert outcome["cancelled"]


def test_disconnect_after_response_arrives_closes_upstream_response():
    """The upstream response lands while the generator is parked on a keepalive yield.

    `_stream_upstream` is never entered, so nothing but the `finally` in
    `_keepalive_stream` can close the response: this is the leak window a client
    disconnect would hit right after llama-server finally sends its headers.
    """

    async def run():
        resp_aclose = AsyncMock()

        async def never_yields():
            await asyncio.sleep(30)
            yield b"never sent"

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.aiter_bytes = never_yields
        mock_response.aclose = resp_aclose

        async def slow_send(request, **kwargs):
            await asyncio.sleep(0.15)
            return mock_response

        with patch("smol_llm_proxy.proxy.SSE_KEEPALIVE_INTERVAL", 0.02):
            with patch("smol_llm_proxy.proxy.reconcile_rate", Mock()):
                with patch("smol_llm_proxy.proxy.enqueue_usage", Mock()):
                    task = asyncio.create_task(slow_send(Mock()))
                    agen = _keepalive_stream(task, {}, {}, "", "", 0, 0)
                    assert await agen.__anext__() == b": keepalive\n\n"
                    # Parked on the keepalive yield; the send completes at ~0.15 s.
                    await asyncio.sleep(0.25)
                    # Disconnect WITHOUT resuming the generator: no byte is ever streamed.
                    await agen.aclose()

        assert resp_aclose.await_count == 1

    asyncio.run(run())


async def _run_disconnect_during_streaming():
    """Helper: disconnect while the generator is suspended at ``yield chunk`` inside _stream_upstream."""
    resp_aclose = AsyncMock()

    async def aiter_then_block():
        # One real SSE chunk, then the upstream hangs (as a model would mid-generation).
        yield (
            b'data: {"id":"1","object":"chat.completion.chunk",'
            b'"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}\n'
        )
        await asyncio.sleep(30)

    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.aiter_bytes = aiter_then_block
    mock_response.aclose = resp_aclose

    async def slow_send(request, **kwargs):
        await asyncio.sleep(0.15)
        return mock_response

    ki = {"id": 1, "name": "k"}
    rt = {"server_id": 1, "server_name": "s"}
    with patch("smol_llm_proxy.proxy.SSE_KEEPALIVE_INTERVAL", 0.02):
        with patch("smol_llm_proxy.proxy.reconcile_rate", Mock()):
            with patch("smol_llm_proxy.proxy.enqueue_usage", Mock()):
                task = asyncio.create_task(slow_send(Mock()))
                agen = _keepalive_stream(task, ki, rt, "m", "m", 1, 1)
                # First yield: keepalive (send_task still pending at 0.15 s).
                first = await agen.__anext__()
                assert first == b": keepalive\n\n"
                # Wait for the send to complete so the keepalive loop exits and
                # _keepalive_stream enters _stream_upstream.
                await asyncio.sleep(0.15)
                # send_task.done() → enters _stream_upstream → yields the real SSE chunk.
                second = await agen.__anext__()
                assert second.startswith(b"data:")
                # Generator is now suspended at ``yield chunk`` inside _stream_upstream.
                await agen.aclose()

    assert resp_aclose.await_count >= 1


def test_client_disconnect_during_streaming_closes_upstream_response():
    """The client goes away while the generator is suspended at ``yield chunk`` inside
    _stream_upstream, i.e. mid-stream → the upstream response is closed."""
    asyncio.run(_run_disconnect_during_streaming())
