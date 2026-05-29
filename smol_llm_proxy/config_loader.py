import os
from pathlib import Path
from .database import get_db
from .cache import set_cached_alias, set_cached_route

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

    servers = cfg.get("servers", [])
    aliases = cfg.get("aliases", {})

    with get_db() as conn:
        # --- Servers ---
        for srv in servers:
            name = srv["name"]
            url = srv["url"]
            api_key = srv.get("api_key", "")

            existing = conn.execute("SELECT id FROM servers WHERE name = ?", (name,)).fetchone()

            if existing:
                server_id = existing["id"]
                # Update URL/api_key if changed
                conn.execute(
                    "UPDATE servers SET url = ?, api_key = ? WHERE id = ?",
                    (url, api_key, server_id),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO servers (name, url, api_key) VALUES (?, ?, ?)",
                    (name, url, api_key),
                )
                server_id = cursor.lastrowid

            # --- Models for this server ---
            for model_name in srv.get("models", []):
                conn.execute(
                    """INSERT INTO server_models (server_id, model_name)
                       SELECT ?, ? WHERE NOT EXISTS (
                           SELECT 1 FROM server_models WHERE server_id = ? AND model_name = ?)""",
                    (server_id, model_name, server_id, model_name),
                )

        # --- Aliases ---
        for alias_name, real_model in aliases.items():
            conn.execute(
                """INSERT INTO model_aliases (alias_name, real_model_name)
                   SELECT ?, ? WHERE NOT EXISTS (
                       SELECT 1 FROM model_aliases WHERE alias_name = ?)""",
                (alias_name, real_model, alias_name),
            )

    # Populate caches from DB
    with get_db() as conn:
        for row in conn.execute("SELECT alias_name, real_model_name FROM model_aliases").fetchall():
            set_cached_alias(row["alias_name"], row["real_model_name"])
        for row in conn.execute(
            """SELECT sm.model_name, s.url, s.api_key FROM server_models sm
               JOIN servers s ON s.id = sm.server_id WHERE s.active = 1"""
        ).fetchall():
            set_cached_route(row["model_name"], {"server_id": 0, "url": row["url"], "api_key": row["api_key"]})
