"""Config sync across restarts: YAML is declarative, keys and history survive."""

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


def _write_yaml(path, content):
    Path(path).write_text(content)


def _api(client, method, path, **kwargs):
    kwargs.setdefault("headers", {"Authorization": f"Bearer {ADMIN_KEY}"})
    return getattr(client, method)(path, **kwargs)


class TestConfigSyncAcrossRestarts:
    """All 10 config-sync cases driven by real uvicorn processes."""

    @pytest.fixture(scope="class")
    def workspace(self):
        d = Path(tempfile.mkdtemp())
        yield d
        shutil.rmtree(d, ignore_errors=True)

    def _boot(self, port, db_path, config_path):
        proc = _start_proxy(port, db_path, config_path)
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)
        return proc, client

    def _teardown(self, proc, client):
        client.close()
        _stop_proxy(proc)

    # --- T1: server: create ---
    def test_server_create(self, workspace):
        port = _find_free_port()
        cfg = workspace / "config.yaml"
        db = workspace / "proxy.db"
        _write_yaml(cfg, "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\naliases: {}\n")
        proc, client = self._boot(port, str(db), str(cfg))
        try:
            resp = _api(client, "get", "/admin/servers")
            assert resp.status_code == 200
            names = [s["name"] for s in resp.json()]
            assert names == ["srv-a"]
        finally:
            self._teardown(proc, client)

    # --- T1: server: change url ---
    def test_server_change_url(self, workspace):
        port = _find_free_port()
        cfg = workspace / "config.yaml"
        db = workspace / "proxy.db"
        _write_yaml(cfg, "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\naliases: {}\n")
        proc, client = self._boot(port, str(db), str(cfg))
        self._teardown(proc, client)

        _write_yaml(cfg, "servers:\n  - name: srv-a\n    url: http://a:8888\n    models:\n      - m1\naliases: {}\n")
        proc, client = self._boot(port, str(db), str(cfg))
        try:
            resp = _api(client, "get", "/admin/servers")
            assert resp.status_code == 200
            urls = [s["url"] for s in resp.json()]
            assert urls == ["http://a:8888"]
        finally:
            self._teardown(proc, client)

    # --- T1: server: api_key set, then absent in YAML ---
    def test_server_api_key_preserved_when_absent(self, workspace):
        port = _find_free_port()
        cfg = workspace / "config.yaml"
        db = workspace / "proxy.db"
        _write_yaml(
            cfg,
            "servers:\n  - name: srv-a\n    url: http://a:9999\n    api_key: SEKRET\n    models:\n      - m1\naliases: {}\n",
        )
        proc, client = self._boot(port, str(db), str(cfg))
        self._teardown(proc, client)

        _write_yaml(cfg, "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\naliases: {}\n")
        proc, client = self._boot(port, str(db), str(cfg))
        try:
            resp = _api(client, "get", "/admin/servers")
            assert resp.status_code == 200
            api_keys = [s["api_key"] for s in resp.json()]
            assert api_keys == ["SEKRET"]
        finally:
            self._teardown(proc, client)

    # --- T1: server: removed from YAML ---
    def test_server_removed_from_yaml(self, workspace):
        port = _find_free_port()
        cfg = workspace / "config.yaml"
        db = workspace / "proxy.db"
        _write_yaml(cfg, "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\naliases: {}\n")
        proc, client = self._boot(port, str(db), str(cfg))
        self._teardown(proc, client)

        _write_yaml(
            cfg,
            "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\n  - name: srv-b\n    url: http://b:9999\n    models:\n      - m2\naliases: {}\n",
        )
        proc, client = self._boot(port, str(db), str(cfg))
        try:
            resp = _api(client, "get", "/admin/servers")
            assert resp.status_code == 200
            names = sorted([s["name"] for s in resp.json()])
            assert names == ["srv-a", "srv-b"]
        finally:
            self._teardown(proc, client)

        _write_yaml(cfg, "servers:\n  - name: srv-b\n    url: http://b:9999\n    models:\n      - m2\naliases: {}\n")
        proc, client = self._boot(port, str(db), str(cfg))
        try:
            resp = _api(client, "get", "/admin/servers")
            assert resp.status_code == 200
            names = [s["name"] for s in resp.json()]
            assert names == ["srv-b"]
        finally:
            self._teardown(proc, client)

    # --- T1: model: assign ---
    def test_model_assign(self, workspace):
        port = _find_free_port()
        cfg = workspace / "config.yaml"
        db = workspace / "proxy.db"
        _write_yaml(cfg, "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\naliases: {}\n")
        proc, client = self._boot(port, str(db), str(cfg))
        try:
            resp = _api(client, "get", "/admin/servers")
            sid = [s["id"] for s in resp.json() if s["name"] == "srv-a"][0]
            import sqlite3

            db_conn = sqlite3.connect(str(db))
            db_conn.row_factory = sqlite3.Row
            _models = [
                r["model_name"]
                for r in db_conn.execute("SELECT model_name FROM server_models WHERE server_id = ?", (sid,)).fetchall()
            ]
            db_conn.close()
            assert _models == ["m1"]
        finally:
            self._teardown(proc, client)

    # --- T1: model: move srv-a -> srv-b ---
    def test_model_move(self, workspace):
        port = _find_free_port()
        cfg = workspace / "config.yaml"
        db = workspace / "proxy.db"
        _write_yaml(
            cfg,
            "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\n  - name: srv-b\n    url: http://b:9999\n    models:\n      - m2\naliases: {}\n",
        )
        proc, client = self._boot(port, str(db), str(cfg))
        self._teardown(proc, client)

        _write_yaml(
            cfg,
            "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m2\n  - name: srv-b\n    url: http://b:9999\n    models:\n      - m1\naliases: {}\n",
        )
        proc, client = self._boot(port, str(db), str(cfg))
        try:
            resp = _api(client, "get", "/admin/servers")
            srv_a_id = [s["id"] for s in resp.json() if s["name"] == "srv-a"][0]
            srv_b_id = [s["id"] for s in resp.json() if s["name"] == "srv-b"][0]
            import sqlite3

            db_conn = sqlite3.connect(str(db))
            db_conn.row_factory = sqlite3.Row
            a_models = [
                r["model_name"]
                for r in db_conn.execute(
                    "SELECT model_name FROM server_models WHERE server_id = ?", (srv_a_id,)
                ).fetchall()
            ]
            b_models = [
                r["model_name"]
                for r in db_conn.execute(
                    "SELECT model_name FROM server_models WHERE server_id = ?", (srv_b_id,)
                ).fetchall()
            ]
            db_conn.close()
            assert a_models == ["m2"]
            assert b_models == ["m1"]
        finally:
            self._teardown(proc, client)

    # --- T1: model: removed from YAML list ---
    def test_model_removed_from_yaml(self, workspace):
        port = _find_free_port()
        cfg = workspace / "config.yaml"
        db = workspace / "proxy.db"
        _write_yaml(cfg, "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\naliases: {}\n")
        proc, client = self._boot(port, str(db), str(cfg))
        self._teardown(proc, client)

        _write_yaml(cfg, "servers:\n  - name: srv-a\n    url: http://a:9999\n    models: []\naliases: {}\n")
        proc, client = self._boot(port, str(db), str(cfg))
        try:
            resp = _api(client, "get", "/admin/servers")
            sid = [s["id"] for s in resp.json() if s["name"] == "srv-a"][0]
            import sqlite3

            db_conn = sqlite3.connect(str(db))
            db_conn.row_factory = sqlite3.Row
            _models = [
                r["model_name"]
                for r in db_conn.execute("SELECT model_name FROM server_models WHERE server_id = ?", (sid,)).fetchall()
            ]
            db_conn.close()
            assert _models == []
        finally:
            self._teardown(proc, client)

    # --- T1: alias: create ---
    def test_alias_create(self, workspace):
        port = _find_free_port()
        cfg = workspace / "config.yaml"
        db = workspace / "proxy.db"
        _write_yaml(
            cfg, "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\naliases:\n  fast: m1\n"
        )
        proc, client = self._boot(port, str(db), str(cfg))
        try:
            resp = _api(client, "get", "/admin/aliases")
            assert resp.status_code == 200
            aliases = {a["alias_name"]: a["real_model_name"] for a in resp.json()}
            assert aliases == {"fast": "m1"}
        finally:
            self._teardown(proc, client)

    # --- T1: alias: retarget ---
    def test_alias_retarget(self, workspace):
        port = _find_free_port()
        cfg = workspace / "config.yaml"
        db = workspace / "proxy.db"
        _write_yaml(
            cfg,
            "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\n      - m2\naliases:\n  fast: m1\n",
        )
        proc, client = self._boot(port, str(db), str(cfg))
        self._teardown(proc, client)

        _write_yaml(
            cfg,
            "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\n      - m2\naliases:\n  fast: m2\n",
        )
        proc, client = self._boot(port, str(db), str(cfg))
        try:
            resp = _api(client, "get", "/admin/aliases")
            aliases = {a["alias_name"]: a["real_model_name"] for a in resp.json()}
            assert aliases == {"fast": "m2"}
        finally:
            self._teardown(proc, client)

    # --- T1: alias: removed from YAML ---
    def test_alias_removed_from_yaml(self, workspace):
        port = _find_free_port()
        cfg = workspace / "config.yaml"
        db = workspace / "proxy.db"
        _write_yaml(
            cfg, "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\naliases:\n  fast: m1\n"
        )
        proc, client = self._boot(port, str(db), str(cfg))
        self._teardown(proc, client)

        _write_yaml(cfg, "servers:\n  - name: srv-a\n    url: http://a:9999\n    models:\n      - m1\naliases: {}\n")
        proc, client = self._boot(port, str(db), str(cfg))
        try:
            resp = _api(client, "get", "/admin/aliases")
            assert resp.status_code == 200
            aliases = [a["alias_name"] for a in resp.json()]
            assert aliases == []
        finally:
            self._teardown(proc, client)
