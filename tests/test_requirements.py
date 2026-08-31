"""Every third-party import must be declared in requirements.txt.

This exists because it did not, once. `python-multipart` was installed in the
development environment by hand, so form posts worked locally and returned a
500 in production, where only requirements.txt is installed. The settings page
and the /foxy slash command were both broken and nothing caught it.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Import name -> distribution name, where they differ.
DIST_FOR_IMPORT = {
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "multipart": "python-multipart",
    "python_multipart": "python-multipart",
    "sqlalchemy": "SQLAlchemy",
    "apscheduler": "APScheduler",
    "psycopg": "psycopg",
    "fastapi": "fastapi",
    "httpx": "httpx",
    "pydantic": "pydantic",
    "uvicorn": "uvicorn",
    "rapidfuzz": "rapidfuzz",
}


def _declared() -> set[str]:
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=\[;]", line)[0].strip().lower()
        if name:
            names.add(name)
    return names


def _third_party_imports() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    found.add(node.module.split(".")[0])
    stdlib = set(sys.stdlib_module_names)
    return {m for m in found if m not in stdlib and m != "app"}


def test_every_import_is_declared():
    declared = _declared()
    missing = []
    for module in sorted(_third_party_imports()):
        dist = DIST_FOR_IMPORT.get(module, module).lower()
        if dist not in declared:
            missing.append(f"{module} (expected '{dist}' in requirements.txt)")
    assert not missing, "undeclared dependencies: " + ", ".join(missing)


def test_form_parsing_dependency_is_declared():
    """Starlette asserts on this the moment `request.form()` is awaited, which
    is a runtime 500 rather than an import error, so it survives a smoke test
    of the home page."""
    assert "python-multipart" in _declared()


def test_form_parsing_actually_works():
    import python_multipart  # noqa: F401

    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).post(
        "/app/no-such-install/save", data={"channel_id": "C1", "action": "save"}
    )
    # An unknown install renders the "not valid" page; the point is that form
    # parsing got far enough to reach the handler at all.
    assert r.status_code < 500
