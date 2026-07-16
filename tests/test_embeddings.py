"""Tests for embeddings endpoint proxy."""

import json
from unittest.mock import patch, AsyncMock


class TestEmbeddingsEndpoint:
    def test_embeddings_proxy(self, server_with_model, admin_key, client):
        """Embeddings endpoint proxied through auth + routing."""
        mock_data = {
            "object": "list",
            "data": [{"index": 0, "object": "embedding", "embedding": [0.1, 0.2, 0.3]}],
        }
        with patch(
            "smol_llm_proxy.proxy._forward_request",
            new=AsyncMock(return_value=(200, json.dumps(mock_data).encode())),
        ):
            resp = client.post(
                "/v1/embeddings",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={"model": server_with_model["model_name"], "input": "hello world"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert len(data["data"]) > 0
