"""Tests for admin API endpoints."""

import time


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


class TestAdminUsageSummary:
    def test_usage_summary_by_alias(self, client, server_setup):
        """GET /admin/usage/summary returns grouped by model_name (alias)."""
        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db

        sid = server_setup["id"]
        model = f"summary-model-{sid}.gguf"

        resp = client.post(
            f"/admin/servers/{sid}/models",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"model_name": model},
        )
        assert resp.status_code == 200

        result = create_api_key("summary-test-user")
        key_id = result["id"]

        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_id, sid, model, model, 10, 5, 15),
            )

        resp = client.get(
            "/admin/usage/summary",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert resp.status_code == 200
        summary = resp.json()
        assert len(summary) >= 1
        assert any(item["model_name"] == model for item in summary)

    def test_usage_summary_by_real(self, client, server_setup):
        """GET /admin/usage/summary/real returns grouped by real_model_name + server_id."""
        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db

        sid = server_setup["id"]
        alias = "my-alias"
        real = f"real-model-{sid}.gguf"

        resp = client.post(
            f"/admin/servers/{sid}/models",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"model_name": real},
        )
        assert resp.status_code == 200

        resp = client.post(
            "/admin/aliases",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"alias_name": alias, "real_model_name": real},
        )
        assert resp.status_code == 200

        result = create_api_key("summary-real-user")
        key_id = result["id"]

        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_id, sid, alias, real, 20, 10, 30),
            )

        resp = client.get(
            "/admin/usage/summary/real",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert resp.status_code == 200
        summary = resp.json()
        assert len(summary) >= 1
        assert any(item["real_model_name"] == real for item in summary)
        assert any(item["server_id"] == sid for item in summary)

    def test_usage_summary_with_date_filter(self, client, server_setup):
        """Usage summary respects start_date/end_date filters on created_at."""
        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db

        sid = server_setup["id"]
        model = f"date-filter-model-{sid}.gguf"

        resp = client.post(
            f"/admin/servers/{sid}/models",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"model_name": model},
        )
        assert resp.status_code == 200

        result = create_api_key("date-filter-user")
        key_id = result["id"]

        now = time.time()
        past_date = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400 * 2))
        future_date = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + 86400))

        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
                (key_id, sid, model, model, 10, 5, 15, str(now - 86400)),
            )

        resp = client.get(
            "/admin/usage/summary",
            headers={"Authorization": "Bearer test-admin-key"},
            params={"start_date": past_date, "end_date": future_date},
        )
        assert resp.status_code == 200
        summary = resp.json()
        assert len(summary) >= 1

        resp2 = client.get(
            "/admin/usage/summary",
            headers={"Authorization": "Bearer test-admin-key"},
            params={"start_date": future_date},
        )
        assert resp2.status_code == 200
        assert resp2.json() == []


class TestAdminUsageLogsLeftJoin:
    def test_usage_visible_after_server_deleted(self, client, server_setup):
        """Usage logs should remain visible after server is deleted (LEFT JOIN)."""
        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db

        sid = server_setup["id"]
        model = f"leftjoin-model-{sid}.gguf"

        resp = client.post(
            f"/admin/servers/{sid}/models",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"model_name": model},
        )
        assert resp.status_code == 200

        result = create_api_key("leftjoin-user")
        key_id = result["id"]

        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_id, sid, model, model, 10, 5, 15),
            )

        # Delete the server
        resp = client.delete(
            f"/admin/servers/{sid}",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert resp.status_code == 200

        # Usage logs should still be visible
        resp = client.get(
            "/admin/usage",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert resp.status_code == 200
        logs = resp.json()["logs"]
        assert len(logs) >= 1
        assert any(log["model_name"] == model for log in logs)
