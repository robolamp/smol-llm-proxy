"""Tests for auth module: API key CRUD and validation."""

import pytest
from smol_llm_proxy.auth import (
    create_api_key, delete_api_key, toggle_api_key, list_api_keys, validate_api_key,
)


def _get_key_id(name):
    keys = list_api_keys()
    return [k for k in keys if k["name"] == name][0]["id"]


class TestAuth:
    def test_create_and_validate(self):
        key = create_api_key("alice")
        assert key.startswith("sk-")
        info = validate_api_key(key)
        assert info is not None and "id" in info

    def test_inactive_key_fails_validation(self, client):
        key = create_api_key("bob")
        toggle_api_key(_get_key_id("bob"), False)
        assert validate_api_key(key) is None

    def test_delete_removes_key(self, client):
        key = create_api_key("charlie")
        assert delete_api_key(_get_key_id("charlie")) is True
        assert validate_api_key(key) is None

    def test_list_returns_all_keys(self, client):
        create_api_key("alice")
        create_api_key("bob")
        keys = list_api_keys()
        names = [k["name"] for k in keys]
        assert "alice" in names and "bob" in names

    def test_delete_nonexistent_returns_false(self):
        assert delete_api_key(99999) is False
