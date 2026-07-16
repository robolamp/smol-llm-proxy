"""Assert line count for smol_llm_proxy package."""

from pathlib import Path


def _is_code_line(lines: list[str]) -> int:
    """Count only non-blank, non-docstring, non-comment code lines in a file."""
    count = 0
    in_docstring = False
    delimiter = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if in_docstring:
            if delimiter in stripped:
                in_docstring = False
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            delimiter = stripped[:3]
            if stripped.count(delimiter) >= 2:
                continue
            in_docstring = True
            continue
        if stripped.startswith("#"):
            continue
        count += 1
    return count


def test_line_count():
    pkg = Path(__file__).parent.parent / "smol_llm_proxy"
    code_lines = 0
    for f in pkg.rglob("*.py"):
        text = f.read_text()
        if _is_code_line(text.splitlines()) > 100:
            print(f"  {f.name}: {_is_code_line(text.splitlines())} lines")
        code_lines += _is_code_line(text.splitlines())
    assert code_lines <= 1100, f"Expected <=1100 code lines, got {code_lines}"
