"""Assert that README documents all routes and env vars from the code."""

import ast
import re
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _readme_text() -> str:
    return (_repo_root() / "README.md").read_text()


def _admin_routes_from_readme(text: str) -> set[str]:
    """Extract admin endpoints from the Admin API table."""
    routes = set()
    in_table = False
    for line in text.splitlines():
        if "Reference: Admin API" in line:
            in_table = True
            continue
        if in_table and line.startswith("|") and line.strip().endswith("|"):
            parts = [p.strip() for p in line.split("|")[1:]]
            if len(parts) >= 2:
                route = parts[0].strip("`")
                if route.startswith("/"):
                    routes.add(route)
        elif in_table and line.strip() == "</details>":
            break
    return routes


def _proxy_routes_from_readme(text: str) -> set[str]:
    """Extract proxy endpoints from the Proxy Endpoints table."""
    routes = set()
    in_table = False
    for line in text.splitlines():
        if "Reference: Proxy Endpoints" in line:
            in_table = True
            continue
        if in_table and line.startswith("|") and line.strip().endswith("|"):
            parts = [p.strip() for p in line.split("|")[1:]]
            if len(parts) >= 2:
                route = parts[0].strip("`")
                if route.startswith("/"):
                    routes.add(route)
        elif in_table and line.strip() == "</details>":
            break
    return routes


def _routes_from_code() -> set[str]:
    """Walk main.py AST to extract route paths."""
    main_py = _repo_root() / "smol_llm_proxy" / "main.py"
    text = main_py.read_text()
    tree = ast.parse(text)
    routes = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "post", "put", "patch", "delete")
        ):
            value = node.func.value
            if isinstance(value, ast.Name) and value.id == "app" and len(node.args) >= 1:
                path_arg = node.args[0]
                if isinstance(path_arg, ast.Constant):
                    routes.add(path_arg.value)
    return routes


def test_readme_documents_all_routes():
    """Every route in main.py must appear in the README Admin API or Proxy Endpoints table."""
    readme_text = _readme_text()
    documented = _admin_routes_from_readme(readme_text) | _proxy_routes_from_readme(readme_text)
    code_routes = _routes_from_code()

    missing = code_routes - documented
    assert not missing, f"Routes in code but not in README: {sorted(missing)}"


def test_readme_has_no_orphan_routes():
    """Every route in the README Admin/Proxy tables must exist in main.py."""
    readme_text = _readme_text()
    documented = _admin_routes_from_readme(readme_text) | _proxy_routes_from_readme(readme_text)
    code_routes = _routes_from_code()

    orphan = documented - code_routes
    assert not orphan, f"Routes in README but not in code: {sorted(orphan)}"


def _env_vars_from_code() -> set[str]:
    """Parse os.environ.get( calls in all package .py files."""
    pkg = _repo_root() / "smol_llm_proxy"
    vars_found = set()
    for py_file in pkg.rglob("*.py"):
        text = py_file.read_text()
        vars_found.update(re.findall(r'os\.environ\.get\(["\'](\w+)["\']', text))
    return vars_found


def _env_vars_from_readme(text: str) -> set[str]:
    """Extract env var names from the README environment variables table."""
    vars_found = set()
    in_table = False
    for line in text.splitlines():
        if "Reference: Environment Variables" in line:
            in_table = True
            continue
        if in_table and line.startswith("|") and line.strip().endswith("|"):
            parts = [p.strip() for p in line.split("|")[1:]]
            if len(parts) >= 1 and parts[0].startswith("`"):
                vars_found.add(parts[0].strip("`"))
        elif in_table and line.strip() == "</details>":
            break
    return vars_found


def test_readme_documents_all_env_vars():
    """Every os.environ.get( name in config.py must appear in the README env var table."""
    readme_text = _readme_text()
    documented = _env_vars_from_readme(readme_text)
    code_vars = _env_vars_from_code()

    missing = code_vars - documented
    assert not missing, f"Env vars in config.py but not in README: {sorted(missing)}"


def test_readme_has_no_orphan_env_vars():
    """Every env var in the README must exist in config.py."""
    readme_text = _readme_text()
    documented = _env_vars_from_readme(readme_text)
    code_vars = _env_vars_from_code()

    orphan = documented - code_vars
    assert not orphan, f"Env vars in README but not in config.py: {sorted(orphan)}"
