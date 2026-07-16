"""Tests for request validation (invalid bodies, missing fields)."""

from unittest.mock import patch, AsyncMock


class TestInvalidBody:
    def test_invalid_json_returns_400(self, client):
        """Non-JSON body should return 400."""
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-invalid"},
            content=b"not json at all",
        )
        assert resp.status_code == 400

    def test_empty_json_body(self, server_with_model, admin_key, client):
        """Empty-ish JSON body still routed through proxy."""
        with patch(
            "smol_llm_proxy.proxy._forward_request",
            new=AsyncMock(
                return_value=(
                    200,
                    b'{"choices":[{"message":{"role":"assistant","content":""}}],"usage":{"prompt_tokens":0,"completion_tokens":0}}',
                )
            ),
        ):
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={"model": server_with_model["model_name"]},
            )
        assert resp.status_code == 200
