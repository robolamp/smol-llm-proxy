"""SQLite database schema and operations."""

import sqlite3
from pathlib import Path
from contextlib import contextmanager

from .config import get_db_path


def _row_to_dict(row):
    return dict(row) if row else None


def _get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = _get_connection(get_db_path())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def validate_key(key_hash: str):
    """Check if a key is valid and active. Returns key info or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name FROM api_keys WHERE key_hash = ? AND active = 1 LIMIT 1",
            (key_hash,),
        ).fetchone()
    return _row_to_dict(row)


def resolve_routing(key_id: int, model_name: str):
    """Resolve alias + find server for a given key. Returns server info or None."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT s.id as server_id, s.url, s.api_key,
                   COALESCE(ma.real_model_name, ?) as real_model
              FROM api_keys ak
              LEFT JOIN model_aliases ma ON ma.alias_name = ?
              JOIN server_models sm ON sm.model_name = COALESCE(ma.real_model_name, ?)
              JOIN servers s ON s.id = sm.server_id
              WHERE ak.id = ? AND s.active = 1
              LIMIT 1""",
            (model_name, model_name, model_name, key_id),
        ).fetchone()
    return _row_to_dict(row)


def init_db():
    get_db_path().parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                url TEXT NOT NULL,
                api_key TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS server_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                model_name TEXT NOT NULL,
                UNIQUE(server_id, model_name)
            );

            CREATE INDEX IF NOT EXISTS idx_server_models_model ON server_models(model_name);

            CREATE TABLE IF NOT EXISTS model_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alias_name TEXT UNIQUE NOT NULL,
                real_model_name TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_model_aliases_alias ON model_aliases(alias_name);

            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

            CREATE TABLE IF NOT EXISTS usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id INTEGER NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
                server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                model_name TEXT NOT NULL,
                real_model_name TEXT NOT NULL DEFAULT '',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                prompt_ms REAL DEFAULT 0,
                predicted_ms REAL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_usage_logs_key ON usage_logs(key_id);
            CREATE INDEX IF NOT EXISTS idx_usage_logs_server ON usage_logs(server_id);
            CREATE INDEX IF NOT EXISTS idx_usage_logs_created ON usage_logs(created_at);
        """)
