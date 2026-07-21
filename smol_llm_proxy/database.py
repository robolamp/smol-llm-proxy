"""SQLite database schema and operations."""

import sqlite3
import threading
from pathlib import Path
from contextlib import contextmanager
from .config import get_db_path
from .cache import get_cached_route, set_cached_route


_thread_local = threading.local()


def _get_connection(db_path: Path) -> sqlite3.Connection:
    """Get or create a thread-local SQLite connection."""
    if not hasattr(_thread_local, "conn") or _thread_local.conn is None:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _thread_local.conn = conn
    return _thread_local.conn


@contextmanager
def get_db():
    """Context manager for DB transactions with auto-commit/rollback."""
    conn = _get_connection(get_db_path())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def resolve_routing(key_id: int, model_name: str) -> dict | None:
    """Resolve alias + find server for a given key. Returns server info or None."""
    cache_key = model_name
    cached = get_cached_route(cache_key)
    if cached:
        return cached

    with get_db() as conn:
        row = conn.execute(
            """SELECT s.id as server_id, s.name as server_name, s.url, s.api_key,
                   COALESCE(ma.real_model_name, ?) as real_model
              FROM api_keys ak
              LEFT JOIN model_aliases ma ON ma.alias_name = ?
              JOIN server_models sm ON sm.model_name = COALESCE(ma.real_model_name, ?)
              JOIN servers s ON s.id = sm.server_id
              WHERE ak.id = ? AND s.active = 1
              LIMIT 1""",
            (model_name, model_name, model_name, key_id),
        ).fetchone()
    result = dict(row) if row else None
    if result:
        set_cached_route(cache_key, result)
    return result


def init_db():
    """Initialize all tables and run migrations."""
    get_db_path().parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS servers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, url TEXT NOT NULL, api_key TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS server_models (id INTEGER PRIMARY KEY AUTOINCREMENT, server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE, model_name TEXT NOT NULL, UNIQUE(model_name));
            CREATE INDEX IF NOT EXISTS idx_server_models_model ON server_models(model_name);
            CREATE TABLE IF NOT EXISTS model_aliases (id INTEGER PRIMARY KEY AUTOINCREMENT, alias_name TEXT UNIQUE NOT NULL, real_model_name TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_model_aliases_alias ON model_aliases(alias_name);
            CREATE TABLE IF NOT EXISTS api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, rpm_limit INTEGER NOT NULL DEFAULT 100, tpm_limit INTEGER NOT NULL DEFAULT 50000, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
            CREATE TABLE IF NOT EXISTS usage_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, key_id INTEGER REFERENCES api_keys(id) ON DELETE SET NULL, server_id REFERENCES servers(id) ON DELETE SET NULL, model_name TEXT NOT NULL, real_model_name TEXT NOT NULL DEFAULT '', prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL DEFAULT 0, prompt_ms REAL DEFAULT 0, predicted_ms REAL DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS idx_usage_logs_key ON usage_logs(key_id);
            CREATE INDEX IF NOT EXISTS idx_usage_logs_server ON usage_logs(server_id);
            CREATE INDEX IF NOT EXISTS idx_usage_logs_created ON usage_logs(created_at);
            CREATE TABLE IF NOT EXISTS rate_limits (id INTEGER PRIMARY KEY AUTOINCREMENT, key_id INTEGER NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE, window_start REAL NOT NULL, request_count INTEGER NOT NULL DEFAULT 0, token_sum INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rate_limits_key_window ON rate_limits(key_id, window_start);
        """)
    # Migration: add rate limit columns if missing
    try:
        with get_db() as conn:
            conn.execute("ALTER TABLE api_keys ADD COLUMN rpm_limit INTEGER NOT NULL DEFAULT 100")
            conn.execute("ALTER TABLE api_keys ADD COLUMN tpm_limit INTEGER NOT NULL DEFAULT 50000")
    except Exception:
        pass
    # Migrate rate_limits index to UNIQUE for UPSERT support.
    try:
        with get_db() as conn:
            already_unique = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index'"
                " AND name='idx_rate_limits_key_window' AND sql LIKE '%UNIQUE%'"
            ).fetchone()
            if not already_unique:
                conn.execute("DROP INDEX IF EXISTS idx_rate_limits_key_window")
                conn.execute("DELETE FROM rate_limits")
                conn.execute("CREATE UNIQUE INDEX idx_rate_limits_key_window ON rate_limits(key_id, window_start)")
    except Exception as e:
        print(f"rate_limits index migration failed: {e}", flush=True)
    # Migration: UNIQUE(server_id, model_name) → UNIQUE(model_name)
    try:
        with get_db() as conn:
            old_unique = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_server_models_model'"
            ).fetchone()
            if old_unique and "UNIQUE" in old_unique["sql"]:
                conn.execute(
                    "DELETE FROM server_models WHERE id NOT IN ("
                    "  SELECT MAX(id) FROM server_models GROUP BY model_name)"
                )
                conn.execute("DROP INDEX IF EXISTS idx_server_models_model")
                conn.execute("CREATE UNIQUE INDEX idx_server_models_model ON server_models(model_name)")
    except Exception:
        pass
    # Migration: denormalize key_name / server_name in usage_logs
    try:
        with get_db() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(usage_logs)").fetchall()}
            for col, fk, tbl in (("key_name", "key_id", "api_keys"), ("server_name", "server_id", "servers")):
                if col not in cols:
                    conn.execute(f"ALTER TABLE usage_logs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
                    conn.execute(
                        f"UPDATE usage_logs SET {col} = (SELECT name FROM {tbl} WHERE id = usage_logs.{fk}) "
                        f"WHERE {fk} IS NOT NULL AND {col} = ''"
                    )
    except Exception:
        pass
