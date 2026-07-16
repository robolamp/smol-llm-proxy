"""Tests for multi-server routing."""


class TestMultipleServersSameModel:
    def test_limit_1_selects_first_server(self, client):
        """When multiple servers have the same model, LIMIT 1 selects one (deterministic by id)."""
        admin_auth = {"Authorization": "Bearer test-admin-key"}
        resp = client.post(
            "/admin/servers", headers=admin_auth, json={"name": "server-a-multi", "url": "http://host-a:8080"}
        )
        assert resp.status_code == 200
        server_a_id = resp.json()["id"]

        resp = client.post(
            "/admin/servers", headers=admin_auth, json={"name": "server-b-multi", "url": "http://host-b:8080"}
        )
        assert resp.status_code == 200
        server_b_id = resp.json()["id"]

        client.post(
            f"/admin/servers/{server_a_id}/models", headers=admin_auth, json={"model_name": "shared-model-multi.gguf"}
        )
        client.post(
            f"/admin/servers/{server_b_id}/models", headers=admin_auth, json={"model_name": "shared-model-multi.gguf"}
        )

        from smol_llm_proxy.auth import create_api_key

        result = create_api_key("multi-server-test")
        key_id = result["id"]

        from smol_llm_proxy.database import resolve_routing

        routing = resolve_routing(key_id, "shared-model-multi.gguf")
        assert routing is not None
        assert routing["server_id"] == server_a_id
