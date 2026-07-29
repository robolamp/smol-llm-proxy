"""Assert that shipped example files stay in sync."""

from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_env_example_sync():
    """Both .env.example copies must be identical."""
    root = _repo_root()
    a = (root / ".env.example").read_text()
    b = (root / "smol_llm_proxy" / ".env.example").read_text()
    assert a == b, ".env.example files differ between root and package"


def test_config_example_sync():
    """Both config.example.yaml copies must be identical."""
    root = _repo_root()
    a = (root / "config.example.yaml").read_text()
    b = (root / "smol_llm_proxy" / "config.example.yaml").read_text()
    assert a == b, "config.example.yaml files differ between root and package"


def test_config_example_has_no_active_server():
    """Shipped config.example.yaml must not register an active server."""
    root = _repo_root()
    content = (root / "config.example.yaml").read_text()
    # The servers: block must be commented out
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("servers:"):
            assert False, "servers: block must be commented out in config.example.yaml"
        if stripped.startswith("aliases:"):
            assert False, "aliases: block must be commented out in config.example.yaml"
