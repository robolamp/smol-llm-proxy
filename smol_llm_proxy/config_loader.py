"""Load and sync configuration from YAML file to database."""

import os
from pathlib import Path

from .database import get_db

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.yaml"))


def _load_yaml():
    """Load config.yaml, return parsed dict or empty dict."""
    try:
        import yaml
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, ImportError):
        return {}


def sync_config():
    """Sync servers, models, and aliases from config.yaml into SQLite.

    - Servers not in DB → created
    - Models not assigned to server → assigned
    - Aliases not in DB → created
    Existing entries are left untouched (idempotent).
    """
    cfg = _load_yaml()

    servers = cfg.get("servers", [])
    aliases = cfg.get("aliases", {})

    with get_db() as conn:
        # --- Servers ---
        for srv in servers:
            name = srv["name"]
            url = srv["url"]
            api_key = srv.get("api_key", "")

            existing = conn.execute(
                "SELECT id FROM servers WHERE name = ?", (name,)
            ).fetchone()

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
