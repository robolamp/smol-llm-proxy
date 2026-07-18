"""Assert line count for smol_llm_proxy package."""

import ast
from pathlib import Path


def _is_code_line(text: str) -> int:
    """Count only non-blank, non-docstring, non-comment code lines via AST."""
    lines = text.splitlines()
    try:
        tree = ast.parse(text, filename="")
    except SyntaxError:
        return len([ln for ln in lines if ln.strip() and not ln.strip().startswith("#")])

    docstring_ranges: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                start = node.body[0].lineno
                end = node.body[0].end_lineno
                for i in range(start, end + 1):
                    docstring_ranges.add(i)

    count = 0
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if i in docstring_ranges:
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
        if _is_code_line(text) > 100:
            print(f"  {f.name}: {_is_code_line(text)} lines")
        code_lines += _is_code_line(text)
    assert code_lines <= 1200, f"Expected <=1200 code lines, got {code_lines}"
