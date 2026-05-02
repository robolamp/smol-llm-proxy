"""API key management: CRUD and validation."""

import secrets
from typing import Optional

from .database import get_db


def _row_to_dict(row) -> dict:
    return dict(row) if row else None


def create_api_key(name: str) -> str:
    """Create a new API key. Returns the key string."""
    key = "sk-" + secrets.token_hex(24)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key, name) VALUES (?, ?)",
            (key, name),
        )
    return key


def delete_api_key(key: str) -> bool:
    """Delete an API key. Returns True if deleted."""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM api_keys WHERE key = ?",
            (key,),
        )
        return cursor.rowcount > 0


def toggle_api_key(key: str, active: bool) -> Optional[dict]:
    """Activate or deactivate an API key. Returns updated key info."""
    with get_db() as conn:
        conn.execute(
            "UPDATE api_keys SET active = ? WHERE key = ?",
            (int(active), key),
        )
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key = ?",
            (key,),
        ).fetchone()
    return _row_to_dict(row)


def get_api_key(key: str) -> Optional[dict]:
    """Look up a single API key."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key = ?",
            (key,),
        ).fetchone()
    return _row_to_dict(row)


def list_api_keys() -> list[dict]:
    """List all API keys."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def validate_api_key(key: str) -> Optional[dict]:
    """Validate an API key and return its info if active."""
    info = get_api_key(key)
    if info and info["active"]:
        return info
    return None
