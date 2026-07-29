"""Tests for auth module: API key CRUD and validation."""

from smol_llm_proxy.auth import (
    _find_key_info_sync,
    create_api_key,
    delete_api_key,
    list_api_keys,
    toggle_api_key,
)


def _get_key_id(name):
    keys = list_api_keys()
    return [k for k in keys if k["name"] == name][0]["id"]


class TestAuth:
    def test_create_and_validate(self):
        result = create_api_key("alice")
        key = result["key"]
        assert key.startswith("sk-")
        info = _find_key_info_sync(key)
        assert info is not None and "id" in info

    def test_inactive_key_fails_validation(self, client):
        result = create_api_key("bob")
        key = result["key"]
        toggle_api_key(_get_key_id("bob"), False)
        info = _find_key_info_sync(key)
        assert info is not None and not info.get("active")

    def test_delete_removes_key(self, client):
        result = create_api_key("charlie")
        key = result["key"]
        assert delete_api_key(_get_key_id("charlie")) is True
        assert _find_key_info_sync(key) is None

    def test_list_returns_all_keys(self, client):
        create_api_key("alice")
        create_api_key("bob")
        keys = list_api_keys()
        names = [k["name"] for k in keys]
        assert "alice" in names and "bob" in names

    def test_delete_nonexistent_returns_false(self):
        assert delete_api_key(99999) is False
