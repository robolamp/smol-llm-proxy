"""Billing attribution survives key/server deletion (T0 acceptance tests)."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

ADMIN_KEY = "test-admin-key"


def _find_free_port():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_proxy(port, db_path, config_path):
    env = os.environ.copy()
    env["ADMIN_KEY"] = ADMIN_KEY
    env["PROXY_PORT"] = str(port)
    env["DB_PATH"] = str(db_path)
    env["CONFIG_PATH"] = str(config_path)
    project_dir = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "smol_llm_proxy.main:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(project_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    timeout = 15
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            if resp.status_code == 200 and resp.json()["status"] == "ok":
                return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError(f"Proxy failed to start on port {port} within {timeout}s")


def _stop_proxy(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


class TestBillingAttribution:
    """key_name and server_name survive deletion of the parent row."""

    @pytest.fixture(scope="class")
    def workspace(self):
        d = Path(tempfile.mkdtemp())
        yield d
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture(scope="class")
    def proxy(self, workspace):
        port = _find_free_port()
        cfg = workspace / "config.yaml"
        db = workspace / "proxy.db"
        _write_yaml(
            cfg, "servers:\n  - name: srv-billing\n    url: http://b:9999\n    models:\n      - m1\naliases: {}\n"
        )
        proc = _start_proxy(port, str(db), str(cfg))
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)
        yield proc, client, str(db)
        client.close()
        _stop_proxy(proc)

    def test_key_name_survives_delete(self, proxy):
        proc, client, db_path = proxy
        # Create key
        resp = client.post(
            "/admin/keys", headers={"Authorization": f"Bearer {ADMIN_KEY}"}, json={"name": "billing-victim"}
        )
        assert resp.status_code == 200
        key_id = resp.json()["id"]

        # Manually insert 3 usage logs with key_name
        import sqlite3

        db = sqlite3.connect(db_path)
        db.execute(
            "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, total_tokens, key_name, server_name) VALUES (?,?,?, ?,12,'billing-victim','srv-billing')",
            (key_id, 1, "m1", "m1"),
        )
        db.execute(
            "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, total_tokens, key_name, server_name) VALUES (?,?,?, ?,12,'billing-victim','srv-billing')",
            (key_id, 1, "m1", "m1"),
        )
        db.execute(
            "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, total_tokens, key_name, server_name) VALUES (?,?,?, ?,12,'billing-victim','srv-billing')",
            (key_id, 1, "m1", "m1"),
        )
        db.commit()
        db.close()

        # Verify key_name is present
        resp = client.get("/admin/usage", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200
        logs = resp.json()["logs"]
        assert len(logs) >= 3
        for log in logs:
            assert log["user_name"] == "billing-victim"

        # Delete the key
        resp = client.delete(f"/admin/keys/{key_id}", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200

        # Verify key_name still present after deletion
        resp = client.get("/admin/usage", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200
        logs = resp.json()["logs"]
        assert len(logs) >= 3
        for log in logs:
            assert log["user_name"] == "billing-victim"

        # Verify summary still attributes tokens
        resp = client.get("/admin/usage", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert any(item.get("key_name") == "billing-victim" for item in summary)

    def test_server_name_survives_delete(self, proxy):
        proc, client, db_path = proxy
        # Get server id
        resp = client.get("/admin/servers", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200
        srv = [s for s in resp.json() if s["name"] == "srv-billing"][0]
        srv_id = srv["id"]

        # Insert usage logs with server_name
        import sqlite3

        db = sqlite3.connect(db_path)
        db.execute(
            "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, total_tokens, key_name, server_name) VALUES (1,?,?, ?,12,'test-key','srv-billing')",
            (srv_id, "m1", "m1"),
        )
        db.execute(
            "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, total_tokens, key_name, server_name) VALUES (1,?,?, ?,12,'test-key','srv-billing')",
            (srv_id, "m1", "m1"),
        )
        db.commit()
        db.close()

        # Verify server_name is present
        resp = client.get("/admin/usage", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200
        logs = resp.json()["logs"]
        assert len(logs) >= 2
        for log in logs:
            assert log["server_name"] == "srv-billing"

        # Delete the server
        resp = client.delete(f"/admin/servers/{srv_id}", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200

        # Verify server_name still present after deletion
        resp = client.get("/admin/usage", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200
        logs = resp.json()["logs"]
        assert len(logs) >= 2
        for log in logs:
            assert log["server_name"] == "srv-billing"


class TestBillingSummaryGroupBy:
    """Two keys on one alias → summary returns two rows with per-key totals."""

    @pytest.fixture(scope="function")
    def proxy_gb(self):
        d = Path(tempfile.mkdtemp())
        port = _find_free_port()
        cfg = d / "config.yaml"
        db = d / "proxy.db"
        _write_yaml(
            cfg,
            "servers:\n"
            "  - name: srv-gb\n"
            "    url: http://b:9999\n"
            "    models:\n"
            "      - m1\n"
            "      - m2\n"
            "aliases:\n"
            "  fast: m1\n"
            "  fast2: m2\n",
        )
        proc = _start_proxy(port, str(db), str(cfg))
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)
        yield proc, client, str(db)
        client.close()
        _stop_proxy(proc)
        shutil.rmtree(d, ignore_errors=True)

    def test_two_keys_same_alias_two_rows(self, proxy_gb):
        proc, client, db_path = proxy_gb

        # Create alice key
        resp = client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": "alice"},
        )
        assert resp.status_code == 200
        alice_id = resp.json()["id"]

        # Create bob key
        resp = client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": "bob"},
        )
        assert resp.status_code == 200
        bob_id = resp.json()["id"]

        # Insert usage logs: alice × 1 request (12 tokens), bob × 5 requests (12 tokens each)
        import sqlite3

        db = sqlite3.connect(db_path)
        db.execute(
            "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, key_name, server_name) VALUES (?,?,?,?, ?, ?, ?, ?, ?)",
            (alice_id, 1, "fast", "m1", 6, 6, 12, "alice", "srv-gb"),
        )
        for i in range(5):
            db.execute(
                "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, key_name, server_name) VALUES (?,?,?,?, ?, ?, ?, ?, ?)",
                (bob_id, 1, "fast", "m1", 6, 6, 12, "bob", "srv-gb"),
            )
        db.commit()
        db.close()

        # Summary must return two rows
        resp = client.get("/admin/usage", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert len(summary) == 2

        # Find each key's row
        alice_row = next((r for r in summary if r["key_name"] == "alice"), None)
        bob_row = next((r for r in summary if r["key_name"] == "bob"), None)
        assert alice_row is not None
        assert bob_row is not None

        # Alice: 1 request, 12 tokens
        assert alice_row["request_count"] == 1
        assert alice_row["total_all_tokens"] == 12

        # Bob: 5 requests, 60 tokens
        assert bob_row["request_count"] == 5
        assert bob_row["total_all_tokens"] == 60

    def test_retargeted_alias_two_real_models(self, proxy_gb):
        proc, client, db_path = proxy_gb

        # Create a key
        resp = client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": "retarget-key"},
        )
        assert resp.status_code == 200
        key_id = resp.json()["id"]

        import sqlite3

        db = sqlite3.connect(db_path)
        # Use old alias target m1
        db.execute(
            "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, key_name, server_name) VALUES (?,?,?,?, ?, ?, ?, ?, ?)",
            (key_id, 1, "fast", "m1", 10, 10, 20, "retarget-key", "srv-gb"),
        )
        # Use new alias target m2
        db.execute(
            "INSERT INTO usage_logs (key_id, server_id, model_name, real_model_name, prompt_tokens, completion_tokens, total_tokens, key_name, server_name) VALUES (?,?,?,?, ?, ?, ?, ?, ?)",
            (key_id, 1, "fast", "m2", 15, 15, 30, "retarget-key", "srv-gb"),
        )
        db.commit()
        db.close()

        # Retarget alias via admin API
        resp = client.patch(
            "/admin/aliases/fast",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"real_model_name": "m2"},
        )
        assert resp.status_code == 200

        # Summary must return two rows with distinct real_model_name
        resp = client.get("/admin/usage", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200
        summary = resp.json()["summary"]
        assert len(summary) == 2

        m1_row = next((r for r in summary if r["real_model_name"] == "m1"), None)
        m2_row = next((r for r in summary if r["real_model_name"] == "m2"), None)
        assert m1_row is not None
        assert m2_row is not None

        # Totals must NOT be merged
        assert m1_row["total_all_tokens"] == 20
        assert m2_row["total_all_tokens"] == 30


def _write_yaml(path, content):
    Path(path).write_text(content)
