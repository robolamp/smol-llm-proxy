"""Assert line count for smol_llm_proxy package."""

from pathlib import Path


def _is_code_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith('"""') or stripped.startswith("'''"):
        return False
    return True


def test_line_count():
    pkg = Path(__file__).parent.parent / "smol_llm_proxy"
    code_lines = 0
    for f in pkg.rglob("*.py"):
        for line in f.read_text().splitlines():
            if _is_code_line(line):
                code_lines += 1
    assert code_lines <= 1000, f"Expected <=1000 code lines, got {code_lines}"
