"""In-memory caches for hot path optimization."""

import time
from typing import Optional

TTL = 30
_bench_cold = False
_key_cache: dict[str, dict] = {}
_alias_cache: dict[str, tuple[str, float]] = {}
_route_cache: dict[str, dict] = {}


def get_cached_key(key_hash: str) -> Optional[dict]:
    if _bench_cold:
        return None
    entry = _key_cache.get(key_hash)
    if entry and entry["_ts"] > time.time() - TTL:
        return {k: v for k, v in entry.items() if k != "_ts"}
    return None


def set_cached_key(key_hash: str, info: dict):
    _key_cache[key_hash] = {**info, "_ts": time.time()}


def clear_key_cache():
    _key_cache.clear()


def get_cached_alias(alias_name: str) -> Optional[str]:
    if _bench_cold:
        return None
    entry = _alias_cache.get(alias_name)
    if entry and entry[1] > time.time() - TTL:
        return entry[0]
    return None


def set_cached_alias(alias_name: str, real_model_name: str):
    _alias_cache[alias_name] = (real_model_name, time.time())


def clear_alias_cache():
    _alias_cache.clear()


def get_cached_route(model_name: str) -> Optional[dict]:
    if _bench_cold:
        return None
    entry = _route_cache.get(model_name)
    if entry and entry["_ts"] > time.time() - TTL:
        return {k: v for k, v in entry.items() if k != "_ts"}
    return None


def set_cached_route(model_name: str, info: dict):
    _route_cache[model_name] = {**info, "_ts": time.time()}


def clear_route_cache():
    _route_cache.clear()


def clear_all():
    clear_key_cache()
    clear_alias_cache()
    clear_route_cache()


def set_bench_cold(value: bool):
    global _bench_cold
    _bench_cold = value
