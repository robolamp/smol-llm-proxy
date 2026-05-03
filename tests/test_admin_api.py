"""Tests for admin API endpoints."""


class TestAdminServers:
    def test_crud_and_duplicate(self, client):
        # Create
        resp = client.post(
            "/admin/servers",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"name": f"srv-{id(client)}", "url": "http://127.0.0.1:8080"},
        )
        assert resp.status_code == 200
        sid = resp.json()["id"]

        # Duplicate name
        resp = client.post(
            "/admin/servers",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"name": f"srv-{id(client)}", "url": "http://127.0.0.1:9090"},
        )
        assert resp.status_code == 409

        # Update URL
        resp = client.patch(
            f"/admin/servers/{sid}",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"url": "http://127.0.0.1:9090"},
        )
        assert resp.status_code == 200

        # Toggle
        resp = client.patch(
            f"/admin/servers/{sid}",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"active": False},
        )
        assert resp.status_code == 200

        # Delete
        resp = client.delete(f"/admin/servers/{sid}", headers={"Authorization": "Bearer test-admin-key"})
        assert resp.status_code == 200

        # Delete nonexistent
        resp = client.delete("/admin/servers/99999", headers={"Authorization": "Bearer test-admin-key"})
        assert resp.status_code == 404


class TestAdminModels:
    def test_assign_and_unassign(self, server_setup, client):
        sid = server_setup["id"]
        model = f"model-{sid}.gguf"

        resp = client.post(
            f"/admin/servers/{sid}/models",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"model_name": model},
        )
        assert resp.status_code == 200

        resp = client.delete(
            f"/admin/servers/{sid}/models/{model}",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert resp.status_code == 200


class TestAdminKeys:
    def test_crud_and_toggle(self, client):
        # Create
        resp = client.post(
            "/admin/keys",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"name": "test-user"},
        )
        assert resp.status_code == 200

        # Get key_id from list
        keys = client.get("/admin/keys", headers={"Authorization": "Bearer test-admin-key"}).json()
        key_id = [k for k in keys if k["name"] == "test-user"][0]["id"]

        # Toggle off
        resp = client.patch(
            f"/admin/keys/{key_id}/toggle",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"active": False},
        )
        assert resp.json()["active"] == 0

        # Toggle on
        resp = client.patch(
            f"/admin/keys/{key_id}/toggle",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"active": True},
        )
        assert resp.json()["active"] == 1

        # Delete
        resp = client.delete(f"/admin/keys/{key_id}", headers={"Authorization": "Bearer test-admin-key"})
        assert resp.status_code == 200


class TestAdminAuth:
    def test_missing_and_wrong_key(self, client):
        resp = client.get("/admin/keys")
        assert resp.status_code == 401

        resp = client.get(
            "/admin/keys",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 403


class TestAdminAliases:
    def test_crud_and_duplicate(self, client):
        alias = f"alias-{id(client)}"

        # Create
        resp = client.post(
            "/admin/aliases",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"alias_name": alias, "real_model_name": "some-model.gguf"},
        )
        assert resp.status_code == 200

        # Duplicate
        resp = client.post(
            "/admin/aliases",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"alias_name": alias, "real_model_name": "other.gguf"},
        )
        assert resp.status_code == 409

        # Missing fields
        resp = client.post(
            "/admin/aliases",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"alias_name": "partial"},
        )
        assert resp.status_code == 400

        # Delete
        resp = client.delete(f"/admin/aliases/{alias}", headers={"Authorization": "Bearer test-admin-key"})
        assert resp.status_code == 200
