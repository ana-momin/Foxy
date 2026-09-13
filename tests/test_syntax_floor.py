"""The codebase has to parse on the oldest Python it claims to support.

A backslash inside an f-string expression is legal from 3.12 and a syntax
error before it. The local interpreter is newer than CI's floor, so the file
imported fine here, the tests passed here, it deployed here - and CI failed on
3.11 with a syntax error in a file nobody had touched in that run.

Checking it here means the feedback arrives in seconds rather than after a
push, and it costs nothing.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

# Anchored to the repository rather than the working directory: run from a
# subdirectory and a relative path here finds nothing and the guard is gone.
ROOT = pathlib.Path(__file__).resolve().parents[1]

# Read from the workflow rather than repeated here, so raising the floor in one
# place cannot leave this test guarding a version nobody runs any more.
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _oldest_supported() -> tuple[int, int]:
    versions = re.findall(r'"(\d+)\.(\d+)"', CI.read_text(encoding="utf-8"))
    assert versions, "the workflow no longer names a python version"
    return min((int(a), int(b)) for a, b in versions)


@pytest.mark.parametrize(
    "path",
    sorted((ROOT / "app").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py")),
    ids=lambda p: str(p.relative_to(ROOT)),
)
def test_the_file_parses_on_the_oldest_supported_python(path: pathlib.Path) -> None:
    floor = _oldest_supported()
    try:
        ast.parse(path.read_text(encoding="utf-8"), str(path), feature_version=floor)
    except SyntaxError as exc:  # pragma: no cover - the message is the point
        pytest.fail(f"{path}:{exc.lineno} is not valid on {floor[0]}.{floor[1]}: {exc.msg}")
