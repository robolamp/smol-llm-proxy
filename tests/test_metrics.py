"""Tests for usage/metrics functionality."""

import time


class TestBuildWhere:
    def test_start_date_maps_to_created_at(self):
        """_build_where maps start_date/end_date to created_at column."""
        from smol_llm_proxy.metrics import _build_where

        filters = {"start_date": "2024-01-01T00:00:00", "end_date": "2024-12-31T23:59:59"}
        where, params = _build_where(filters)

        assert "created_at >= ?" in where
        assert "created_at <= ?" in where
        assert params == ["2024-01-01T00:00:00", "2024-12-31T23:59:59"]

    def test_key_id_and_server_id_still_work(self):
        from smol_llm_proxy.metrics import _build_where

        filters = {"key_id": 5, "server_id": 3}
        where, params = _build_where(filters)

        assert "key_id = ?" in where
        assert "server_id = ?" in where
        assert params == [5, 3]

    def test_combined_filters(self):
        from smol_llm_proxy.metrics import _build_where

        filters = {"key_id": 1, "start_date": "2024-06-01T00:00:00", "end_date": "2024-06-30T23:59:59"}
        where, params = _build_where(filters)

        assert "key_id = ?" in where
        assert "created_at >= ?" in where
        assert "created_at <= ?" in where
        assert params == [1, "2024-06-01T00:00:00", "2024-06-30T23:59:59"]

    def test_empty_filters(self):
        from smol_llm_proxy.metrics import _build_where

        where, params = _build_where({})
        assert where == ""
        assert params == []


class TestUsageSummaryByReal:
    def test_groups_by_real_model_and_server(self, client, server_setup):
        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db
        from smol_llm_proxy.metrics import get_usage_summary_by_real

        sid = server_setup["id"]
        real = f"real-summary-{sid}.gguf"

        resp = client.post(
            f"/admin/servers/{sid}/models",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"model_name": real},
        )
        assert resp.status_code == 200

        result = create_api_key("real-summary-user")
        key_id = result["id"]

        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_id, sid, "alias-a", real, 20, 10, 30),
            )
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_id, sid, "alias-b", real, 15, 8, 23),
            )

        summary = get_usage_summary_by_real(key_id=key_id)
        assert len(summary) >= 1
        # Both aliases should be aggregated under the same real_model_name
        real_entry = [item for item in summary if item["real_model_name"] == real]
        assert len(real_entry) >= 1
        assert real_entry[0]["request_count"] == 2
        assert real_entry[0]["total_prompt_tokens"] == 35
        assert real_entry[0]["total_completion_tokens"] == 18

    def test_with_date_filter(self, client, server_setup):
        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db
        from smol_llm_proxy.metrics import get_usage_summary_by_real

        sid = server_setup["id"]
        real = f"real-date-{sid}.gguf"

        resp = client.post(
            f"/admin/servers/{sid}/models",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"model_name": real},
        )
        assert resp.status_code == 200

        result = create_api_key("real-date-user")
        key_id = result["id"]

        now = time.time()
        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
                (key_id, sid, "alias", real, 10, 5, 15, str(now - 86400)),
            )

        past = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400 * 2))
        future = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + 86400))

        summary = get_usage_summary_by_real(key_id=key_id, start_date=past, end_date=future)
        assert len(summary) >= 1

        summary2 = get_usage_summary_by_real(key_id=key_id, start_date=future)
        assert summary2 == []


class TestAdminUsageDateFilters:
    """T2: /admin/usage?start_date= / ?end_date= must return 200, not 500."""

    def test_start_date_filter_returns_200(self, client, server_setup):
        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db

        sid = server_setup["id"]
        result = create_api_key("date-filter-user")
        key_id = result["id"]

        now = time.time()
        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
                (key_id, sid, "alias", "real.gguf", 10, 5, 15, str(now - 86400)),
            )

        future_date = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + 86400))
        resp = client.get(f"/admin/usage?start_date={future_date}", headers={"Authorization": "Bearer test-admin-key"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["logs"]) == 0

    def test_end_date_filter_returns_200(self, client, server_setup):
        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db

        sid = server_setup["id"]
        result = create_api_key("end-date-user")
        key_id = result["id"]

        now = time.time()
        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
                (key_id, sid, "alias", "real.gguf", 10, 5, 15, str(now - 86400)),
            )

        past_date = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400 * 2))
        resp = client.get(f"/admin/usage?end_date={past_date}", headers={"Authorization": "Bearer test-admin-key"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["logs"]) == 0

    def test_start_and_end_date_filter_returns_200(self, client, server_setup):
        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db

        sid = server_setup["id"]
        result = create_api_key("range-date-user")
        key_id = result["id"]

        now = time.time()
        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
                (key_id, sid, "alias", "real.gguf", 10, 5, 15, str(now - 86400)),
            )

        past = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400 * 2))
        future = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + 86400))
        resp = client.get(
            f"/admin/usage?start_date={past}&end_date={future}",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["logs"]) >= 1

    def test_key_id_with_start_date_filter_returns_200(self, client, server_setup):
        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db

        sid = server_setup["id"]
        result = create_api_key("key-date-user")
        key_id = result["id"]

        now = time.time()
        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
                (key_id, sid, "alias", "real.gguf", 10, 5, 15, str(now - 86400)),
            )

        future_date = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + 86400))
        resp = client.get(
            f"/admin/usage?key_id={key_id}&start_date={future_date}",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["logs"]) == 0

    def test_start_date_excludes_out_of_range(self, client, server_setup):
        from smol_llm_proxy.auth import create_api_key
        from smol_llm_proxy.database import get_db

        sid = server_setup["id"]
        result = create_api_key("exclude-user-3")
        key_id = result["id"]

        now = time.time()
        with get_db() as conn:
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
                (key_id, sid, "alias-a", "real.gguf", 10, 5, 15, str(now - 86400)),
            )
            conn.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime(?, 'unixepoch'))",
                (key_id, sid, "alias-b", "real.gguf", 20, 10, 30, str(now - 172800)),
            )

        start = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 86400 * 1.2))
        end = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now + 86400))
        resp = client.get(
            f"/admin/usage?key_id={key_id}&start_date={start}&end_date={end}",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["logs"]) == 1
        assert data["logs"][0]["prompt_tokens"] == 10
