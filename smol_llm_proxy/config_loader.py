import os
from pathlib import Path
from .database import get_db
from .cache import set_cached_alias

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
        for srv in cfg.get("servers", []):
            if not isinstance(srv, dict):
                continue
            name, url = srv.get("name"), srv.get("url")
            if not name or not url:
                continue
            api_key = srv.get("api_key", "")
            existing = conn.execute("SELECT id FROM servers WHERE name = ?", (name,)).fetchone()
            if existing:
                conn.execute("UPDATE servers SET url = ?, api_key = ? WHERE id = ?", (url, api_key, existing["id"]))
                server_id = existing["id"]
            else:
                server_id = conn.execute(
                    "INSERT INTO servers (name, url, api_key) VALUES (?, ?, ?)", (name, url, api_key)
                ).lastrowid
            for model_name in srv.get("models", []):
                dup = conn.execute(
                    "SELECT sm.server_id FROM server_models sm WHERE sm.model_name = ?", (model_name,)
                ).fetchone()
                if dup and dup["server_id"] != server_id:
                    print(f"config_loader: model '{model_name}' on server {dup['server_id']}, skipping", flush=True)
                elif not dup:
                    conn.execute(
                        "INSERT INTO server_models (server_id, model_name) VALUES (?, ?)",
                        (server_id, model_name),
                    )
        for alias_name, real_model in cfg.get("aliases", {}).items():
            existing = conn.execute("SELECT id FROM model_aliases WHERE alias_name = ?", (alias_name,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE model_aliases SET real_model_name = ? WHERE alias_name = ?",
                    (real_model, alias_name),
                )
            else:
                conn.execute(
                    "INSERT INTO model_aliases (alias_name, real_model_name) VALUES (?, ?)",
                    (alias_name, real_model),
                )
    with get_db() as conn:
        for row in conn.execute("SELECT alias_name, real_model_name FROM model_aliases").fetchall():
            set_cached_alias(row["alias_name"], row["real_model_name"])
