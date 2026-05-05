"""API key management: CRUD and validation with SHA256 hashing."""

import hashlib
import secrets

from .database import get_db
from .cache import get_cached_key, set_cached_key, clear_key_cache


def _row_to_dict(row) -> dict:
    return dict(row) if row else None


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def create_api_key(name: str) -> dict:
    """Create a new API key. Returns dict with id, key, and name."""
    raw_key = "sk-" + secrets.token_hex(24)
    key_hash = _hash_key(raw_key)

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO api_keys (key_hash, name) VALUES (?, ?)",
            (key_hash, name),
        )
        key_id = cursor.lastrowid
    clear_key_cache()
    return {"id": key_id, "key": raw_key, "name": name}


def _find_key_info(raw_key: str):
    """Find key info by direct hash lookup."""
    key_hash = _hash_key(raw_key)
    cached = get_cached_key(key_hash)
    if cached:
        return cached

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, key_hash, name, active FROM api_keys WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
    info = _row_to_dict(row)
    if info:
        set_cached_key(key_hash, info)
    return info


def delete_api_key(key_id: int) -> bool:
    """Delete an API key by its database id."""
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    if cursor.rowcount > 0:
        clear_key_cache()
    return cursor.rowcount > 0


def toggle_api_key(key_id: int, active: bool):
    """Activate or deactivate an API key."""
    with get_db() as conn:
        conn.execute(
            "UPDATE api_keys SET active = ? WHERE id = ?",
            (int(active), key_id),
        )
        row = conn.execute(
            "SELECT id, name, active, created_at FROM api_keys WHERE id = ?",
            (key_id,),
        ).fetchone()
    clear_key_cache()
    return _row_to_dict(row)


def list_api_keys() -> list[dict]:
    """List all API keys (never returns key hashes)."""
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, active, created_at FROM api_keys ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def validate_api_key(raw_key: str):
    """Validate an API key by SHA256 hash lookup."""
    info = _find_key_info(raw_key)
    if info and info.get("active"):
        return {"id": info["id"], "name": info["name"]}
    return None
