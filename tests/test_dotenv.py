"""Tests for .env file loading."""

import os

import pytest
from smol_llm_proxy.config import _load_dotenv


class TestDotenvLoading:
    @pytest.fixture(autouse=True)
    def _clear_env(self):
        keys = (
            "ADMIN_KEY",
            "PROXY_PORT",
            "PROXY_HOST",
            "DB_PATH",
            "CONFIG_PATH",
            "HTTPX_TIMEOUT",
            "PROXY_MAX_CONNECTIONS",
            "PROXY_MAX_KEEPALIVE",
            "PROXY_KEEPALIVE_EXPIRY",
            "BENCH_COLD_CACHE",
        )
        saved = {k: os.environ.get(k) for k in keys if k in os.environ}
        for k in keys:
            os.environ.pop(k, None)
        yield
        for k, v in saved.items():
            os.environ[k] = v
        # Restore conftest values
        os.environ["ADMIN_KEY"] = "test-admin-key"
        os.environ["PROXY_PORT"] = "8099"

    def test_env_file_is_read_when_present(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("ADMIN_KEY=from-env-file\nPROXY_PORT=9999\n")
        _load_dotenv(env_file)
        assert os.environ["ADMIN_KEY"] == "from-env-file"
        assert os.environ["PROXY_PORT"] == "9999"

    def test_real_env_var_overrides_dotenv(self, tmp_path):
        os.environ["ADMIN_KEY"] = "from-real-env"
        env_file = tmp_path / ".env"
        env_file.write_text("ADMIN_KEY=from-env-file\n")
        _load_dotenv(env_file)
        assert os.environ["ADMIN_KEY"] == "from-real-env"

    def test_no_env_file_does_not_crash(self, tmp_path):
        # No .env file exists at tmp_path / ".env"
        _load_dotenv(tmp_path / ".env")
        # Must not raise
        assert True

    def test_quoted_values_are_stripped(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("ADMIN_KEY='my-secret'\n")
        _load_dotenv(env_file)
        assert os.environ["ADMIN_KEY"] == "my-secret"

    def test_comment_lines_are_ignored(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# ADMIN_KEY=should-not-appear\nADMIN_KEY=real-key\n")
        _load_dotenv(env_file)
        assert os.environ["ADMIN_KEY"] == "real-key"

    def test_blank_lines_are_skipped(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("\nADMIN_KEY=with-blanks\n\n")
        _load_dotenv(env_file)
        assert os.environ["ADMIN_KEY"] == "with-blanks"

    def test_setdefault_preserves_existing_env(self, tmp_path):
        os.environ["ADMIN_KEY"] = "env-wins"
        env_file = tmp_path / ".env"
        env_file.write_text("ADMIN_KEY=file-loses\n")
        _load_dotenv(env_file)
        assert os.environ["ADMIN_KEY"] == "env-wins"
