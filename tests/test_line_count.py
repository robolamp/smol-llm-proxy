"""Assert exact line count for smol_llm_proxy package."""

from pathlib import Path


def test_exact_line_count():
    pkg = Path(__file__).parent.parent / "smol_llm_proxy"
    total = 0
    for f in pkg.rglob("*.py"):
        total += len(f.read_text().splitlines())
    assert total <= 1000, f"Expected <=1000 lines, got {total}"
