"""Guard the numbers the docs claim about this project.

Every count in the README has drifted at least once — the tool count sat at 16
and 18 in different files while the code had 17, and the test count was quoted as
429 long after it passed 500. A reader cannot tell a stale number from a wrong
one, and in a repo whose selling point is "the arithmetic is trustworthy" that is
a worse look than it sounds.

These tests derive the truth from the code and the filesystem rather than
restating it, so the docs cannot drift without a failure here.
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ifta.agent.tools import ALL_TOOLS

ROOT = Path(__file__).resolve().parent.parent
DOCS = [
    ROOT / "README.md",
    ROOT / "docs" / "design.md",
    ROOT / "docs" / "TRACING.md",
    ROOT / "ifta-portfolio" / "CASE-STUDY.md",
]
# Matches "17 tools", "17 grounded tools", a count wrapped onto the next line,
# and the hyphenated "17-tool agent" form — one stale claim survived a manual
# grep in each of those shapes.
_TOOL_COUNT = re.compile(r"(\d+)[\s-]+(?:grounded\s+)?tools?\b")
_TEST_COUNT = re.compile(r"(\d+)\s+(?:automated\s+)?tests\b")


@pytest.mark.parametrize("doc", [d for d in DOCS if d.exists()], ids=lambda d: d.name)
def test_documented_tool_count_matches_the_code(doc: Path) -> None:
    claimed = {int(m) for m in _TOOL_COUNT.findall(doc.read_text(encoding="utf-8"))}
    assert claimed <= {len(ALL_TOOLS)}, (
        f"{doc.name} claims {sorted(claimed)} tools; the code defines {len(ALL_TOOLS)}"
    )


@pytest.mark.parametrize("doc", [d for d in DOCS if d.exists()], ids=lambda d: d.name)
def test_documented_test_count_matches_the_suite(doc: Path) -> None:
    """Allow a lag, because the count moves with every merged branch.

    The band is wide on purpose. The docs quote the number main will carry once
    the branches in flight land, so on any single branch the figure is legitimately
    off by a few dozen. What this catches is the failure that actually happened
    here: a count stale by *hundreds* (429 quoted while the suite was past 500),
    which tells a reader nothing except that nobody checks.
    """
    actual = _collected_test_count()
    for claimed in (int(m) for m in _TEST_COUNT.findall(doc.read_text(encoding="utf-8"))):
        assert abs(claimed - actual) <= 60, (
            f"{doc.name} claims {claimed} tests; the suite collects {actual}"
        )


def test_documented_test_file_count_matches_the_tree() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    actual = len(list((ROOT / "tests").glob("test_*.py")))
    for claimed in (int(m) for m in re.findall(r"across (\d+) files", readme)):
        # Same reasoning as the test count: branches in flight add files.
        assert abs(claimed - actual) <= 4, (
            f"README claims {claimed} test files; the tree has {actual}"
        )


@functools.lru_cache(maxsize=1)
def _collected_test_count() -> int:
    """Ask pytest, so the documented number is the one a reader reproduces.

    Counting `def test_` lines instead would undercount every parametrized case,
    which is most of the difference between "46 test functions" and what
    `pytest` prints. Collection only (no test bodies run) and takes well under a
    second; cached so the parametrized cases above pay for it once.
    """
    proc = subprocess.run(
        # No -q: pyproject's addopts already carries one, and a second turns the
        # summary line into per-file counts with no total to read.
        [sys.executable, "-m", "pytest", "--collect-only", str(ROOT / "tests")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    if not match:
        pytest.skip(f"could not read a collection count from pytest: {proc.stdout[-200:]}")
    return int(match.group(1))
