"""The pages and the files they pull in.

A console is HTML plus the script that makes it act. The HTML is covered well
by the rest of the suite; the script was not covered at all, and so a commit
that truncated it to nothing shipped, and every button on the console quietly
went back to reloading the page. Nothing failed, which is why nobody noticed.

So: every asset a page asks for must arrive with something in it, and every
element a script reaches for must exist in the markup it runs against.
"""

from __future__ import annotations

import os
import pathlib
import re
import tempfile

import pytest

os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-the-suite")

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "static"
ADMIN = "admin-key-for-the-suite"

ASSET = re.compile(r'(?:src|href)="(/assets/[^"]+)"')
BY_ID = re.compile(r'getElementById\("([^"]+)"\)')
FIELD = re.compile(r'\bform\.([a-z_][a-z0-9_]*)\.value\b')


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings

    monkeypatch.setattr(
        settings,
        "database_url",
        f"sqlite:///{pathlib.Path(tempfile.mkdtemp()) / 'assets.db'}",
    )
    import app.db as database

    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_schema_ready", False)
    from app import installs  # noqa: F401  registers the table

    database.init_db()

    monkeypatch.setattr(settings, "admin_key", ADMIN)

    from app.main import app

    return TestClient(app)


def pages(client) -> dict[str, str]:
    """Every page a person can land on, rendered."""
    out = {}
    for url in ("/", "/how", "/pricing", "/setup", f"/admin?key={ADMIN}"):
        r = client.get(url)
        assert r.status_code == 200, f"{url} answered {r.status_code}"
        out[url] = r.text
    return out


def test_every_asset_a_page_asks_for_arrives_with_something_in_it(client):
    asked = set()
    for url, html in pages(client).items():
        for path in ASSET.findall(html):
            asked.add((url, path))

    assert asked, "no page referenced an asset - this test stopped testing"

    for url, path in sorted(asked):
        r = client.get(path)
        assert r.status_code == 200, (
            f"{url} asks for {path}, which answers {r.status_code}"
        )
        assert r.content.strip(), f"{url} asks for {path}, which is empty"


def test_no_script_is_shipped_empty():
    scripts = sorted(STATIC.glob("*.js"))
    assert scripts, "no scripts found - this test stopped testing"
    for path in scripts:
        text = path.read_text(encoding="utf-8").strip()
        assert text, f"{path.name} is empty"
        # An accidental truncation can leave a comment header behind, which is
        # bytes without behaviour. Insist on something that runs.
        assert "function" in text, f"{path.name} has no code in it"


def test_every_element_a_script_reaches_for_exists(client):
    markup = "\n".join(pages(client).values())

    for path in sorted(STATIC.glob("*.js")):
        source = path.read_text(encoding="utf-8")
        for name in sorted(set(BY_ID.findall(source))):
            assert f'id="{name}"' in markup, (
                f"{path.name} reaches for #{name}, which no page renders"
            )


def test_every_form_field_a_script_reads_exists(client):
    """preview.js reads the announcement form by field name.

    A renamed field would not raise on the server; it would make the preview
    read undefined and stop drawing, silently.
    """
    markup = "\n".join(pages(client).values())

    for path in sorted(STATIC.glob("*.js")):
        source = path.read_text(encoding="utf-8")
        for name in sorted(set(FIELD.findall(source))):
            assert f'name="{name}"' in markup, (
                f"{path.name} reads form.{name}, which no page renders"
            )


def test_the_console_still_asks_for_its_script(client):
    html = client.get(f"/admin?key={ADMIN}").text
    assert "/assets/admin.js" in html, "the console lost the script that makes it act"
