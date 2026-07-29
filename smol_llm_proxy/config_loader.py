import os
from pathlib import Path

from .database import get_db

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.yaml"))


def _load_yaml():
    try:
        import yaml

        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, ImportError):
        return {}


def sync_config():
    cfg = _load_yaml()
    with get_db() as conn:
        yaml_servers = {}
        for srv in cfg.get("servers", []):
            if not isinstance(srv, dict):
                continue
            name, url = srv.get("name"), srv.get("url")
            if not name or not url:
                continue
            yaml_servers[name] = srv
        for name, srv in yaml_servers.items():
            url = srv["url"]
            existing = conn.execute("SELECT id FROM servers WHERE name = ?", (name,)).fetchone()
            if existing:
                sid = existing["id"]
                if "api_key" in srv:
                    conn.execute("UPDATE servers SET url = ?, api_key = ? WHERE id = ?", (url, srv["api_key"], sid))
                else:
                    conn.execute("UPDATE servers SET url = ? WHERE id = ?", (url, sid))
            else:
                sid = conn.execute(
                    "INSERT INTO servers (name, url, api_key) VALUES (?, ?, ?)", (name, url, srv.get("api_key", ""))
                ).lastrowid
            yaml_models = set(srv.get("models", []))
            for mn in yaml_models:
                dup = conn.execute(
                    "SELECT sm.server_id FROM server_models sm WHERE sm.model_name = ?", (mn,)
                ).fetchone()
                if dup:
                    if dup["server_id"] != sid:
                        conn.execute("DELETE FROM server_models WHERE model_name = ?", (mn,))
                        conn.execute("INSERT INTO server_models (server_id, model_name) VALUES (?, ?)", (sid, mn))
                else:
                    conn.execute("INSERT INTO server_models (server_id, model_name) VALUES (?, ?)", (sid, mn))
            for row in conn.execute("SELECT model_name FROM server_models WHERE server_id = ?", (sid,)).fetchall():
                if row["model_name"] not in yaml_models:
                    conn.execute(
                        "DELETE FROM server_models WHERE server_id = ? AND model_name = ?", (sid, row["model_name"])
                    )
        yaml_aliases = cfg.get("aliases", {})
        for an, rm in yaml_aliases.items():
            existing = conn.execute("SELECT id FROM model_aliases WHERE alias_name = ?", (an,)).fetchone()
            if existing:
                conn.execute("UPDATE model_aliases SET real_model_name = ? WHERE alias_name = ?", (rm, an))
            else:
                conn.execute("INSERT INTO model_aliases (alias_name, real_model_name) VALUES (?, ?)", (an, rm))
    from .cache import clear_route_cache

    clear_route_cache()
