"""API key management: CRUD and validation with bcrypt hashing."""

import secrets
import bcrypt
from typing import Optional

from .database import get_db


def _row_to_dict(row) -> dict:
    return dict(row) if row else None


def create_api_key(name: str) -> str:
    """Create a new API key. Returns the plaintext key once (shown only at creation)."""
    raw_key = "sk-" + secrets.token_hex(24)
    key_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt()).decode()

    with get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, name) VALUES (?, ?)",
            (key_hash, name),
        )
    return raw_key


def _find_key_info(raw_key: str):
    """Find key info by checking hash against all stored hashes."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, key_hash, name, active FROM api_keys"
        ).fetchall()

    for r in rows:
        stored_hash = r["key_hash"].encode() if isinstance(r["key_hash"], str) else r["key_hash"]
        if bcrypt.checkpw(raw_key.encode(), stored_hash):
            return {"id": r["id"], "name": r["name"], "active": bool(r["active"]), "key_hash": stored_hash}
    return None


def delete_api_key(raw_key: str) -> bool:
    """Delete an API key by its plaintext value."""
    info = _find_key_info(raw_key)
    if not info:
        return False

    with get_db() as conn:
        cursor = conn.execute("DELETE FROM api_keys WHERE id = ?", (info["id"],))
    return cursor.rowcount > 0


def toggle_api_key(raw_key: str, active: bool) -> Optional[dict]:
    """Activate or deactivate an API key."""
    info = _find_key_info(raw_key)
    if not info:
        return None

    with get_db() as conn:
        conn.execute(
            "UPDATE api_keys SET active = ? WHERE id = ?",
            (int(active), info["id"]),
        )
        row = conn.execute(
            "SELECT id, name, active, created_at FROM api_keys WHERE id = ?",
            (info["id"],),
        ).fetchone()
    return _row_to_dict(row)


def list_api_keys() -> list[dict]:
    """List all API keys (never returns key hashes)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, active, created_at FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def validate_api_key(raw_key: str) -> Optional[dict]:
    """Validate an API key by comparing bcrypt hash."""
    info = _find_key_info(raw_key)
    if info and info["active"]:
        return {"id": info["id"], "name": info["name"]}
    return None
