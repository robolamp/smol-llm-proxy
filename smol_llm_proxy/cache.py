"""In-memory caches for hot path optimization."""

import time

TTL = 30
_bench_cold = False
_key_cache: dict[str, dict] = {}
_route_cache: dict[str, dict] = {}


def get_cached_key(key_hash: str) -> dict | None:
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


def get_cached_route(model_name: str) -> dict | None:
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


def set_bench_cold(value: bool):
    global _bench_cold
    _bench_cold = value
