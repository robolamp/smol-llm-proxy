"""Tests for multi-server routing."""


class TestMultipleServersSameModel:
    def test_duplicate_model_returns_409(self, client):
        """When the same model is assigned to two servers, the second returns 409."""
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

        resp = client.post(
            f"/admin/servers/{server_a_id}/models", headers=admin_auth, json={"model_name": "shared-model-multi.gguf"}
        )
        assert resp.status_code == 200

        resp = client.post(
            f"/admin/servers/{server_b_id}/models", headers=admin_auth, json={"model_name": "shared-model-multi.gguf"}
        )
        assert resp.status_code == 409
        assert "already assigned" in resp.json()["detail"].lower()


class TestModelReassign:
    def test_reassign_moves_model_to_different_server(self, client):
        """?reassign=true moves a model from one server to another."""
        admin_auth = {"Authorization": "Bearer test-admin-key"}

        resp = client.post("/admin/servers", headers=admin_auth, json={"name": "server-x", "url": "http://host-x:8080"})
        assert resp.status_code == 200
        server_x_id = resp.json()["id"]

        resp = client.post("/admin/servers", headers=admin_auth, json={"name": "server-y", "url": "http://host-y:8080"})
        assert resp.status_code == 200
        server_y_id = resp.json()["id"]

        resp = client.post(
            f"/admin/servers/{server_x_id}/models", headers=admin_auth, json={"model_name": "reassign-model.gguf"}
        )
        assert resp.status_code == 200

        resp = client.post(
            f"/admin/servers/{server_y_id}/models",
            headers=admin_auth,
            json={"model_name": "reassign-model.gguf"},
            params={"reassign": "true"},
        )
        assert resp.status_code == 200

        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db, resolve_routing

        result = create_api_key("reassign-test")
        key_id = result["id"]

        routing = resolve_routing(key_id, "reassign-model.gguf")
        assert routing is not None
        assert routing["server_id"] == server_y_id

        # Old assignment should be gone
        with get_db() as conn:
            old = conn.execute(
                "SELECT 1 FROM server_models WHERE server_id = ? AND model_name = ?",
                (server_x_id, "reassign-model.gguf"),
            ).fetchone()
            assert old is None


class TestSingleServerPerModel:
    def test_model_resolved_to_only_server(self, client):
        """A model assigned to exactly one server resolves correctly."""
        admin_auth = {"Authorization": "Bearer test-admin-key"}

        resp = client.post(
            "/admin/servers", headers=admin_auth, json={"name": "single-server", "url": "http://host:8080"}
        )
        assert resp.status_code == 200
        server_id = resp.json()["id"]

        resp = client.post(
            f"/admin/servers/{server_id}/models", headers=admin_auth, json={"model_name": "unique-model.gguf"}
        )
        assert resp.status_code == 200

        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import resolve_routing

        result = create_api_key("single-test")
        key_id = result["id"]

        routing = resolve_routing(key_id, "unique-model.gguf")
        assert routing is not None
        assert routing["server_id"] == server_id
