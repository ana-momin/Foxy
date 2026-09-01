"""Validate the manifest against Pond's own published schema.

This exists because the manifest was invalid for days without anything saying
so. Pond reported "the manifest, runs, and tasks endpoints could not be found",
which reads like a routing problem; the endpoints were fine, and the manifest
simply failed schema validation. Four faults, all silent:

  * limits.max_attachment_bytes was missing (required)
  * limits.max_run_seconds was missing (required)
  * limits.max_execution_ms was invented, and limits forbids extra properties
  * an array item schema had no description, which valueSchema requires

The schema in tests/data is copied verbatim from the appendix of
docs.joinpond.ai/docs/build-and-publish-an-agent-on-pond-full.
"""

from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest

from app.main import manifest

SCHEMA = json.loads(
    (pathlib.Path(__file__).parent / "data" / "pond-manifest-schema.json").read_text(
        encoding="utf-8"
    )
)


def test_manifest_validates_against_ponds_schema():
    errors = sorted(
        jsonschema.Draft202012Validator(SCHEMA).iter_errors(manifest()),
        key=lambda e: list(e.path),
    )
    if errors:
        report = "\n".join(
            f"  {'/'.join(str(p) for p in e.path) or '(root)'}: {e.message}"
            for e in errors
        )
        pytest.fail(f"{len(errors)} schema violations:\n{report}")


def test_limits_uses_the_exact_field_names():
    """`limits` sets additionalProperties: false, so a plausible-looking name
    like max_execution_ms invalidates the whole manifest."""
    limits = manifest()["limits"]
    assert set(limits) == {
        "max_request_bytes",
        "max_attachment_bytes",
        "max_run_seconds",
    }
    assert all(isinstance(v, int) and v >= 1 for v in limits.values())


def test_capabilities_declares_every_flag():
    caps = manifest()["capabilities"]
    assert set(caps) == {
        "sync",
        "streaming",
        "async_tasks",
        "cancellation",
        "attachments",
        "feedback",
    }


def test_async_tasks_matches_the_tasks_endpoint():
    """Declaring async_tasks commits the agent to serving GET /tasks/{id}.

    Checked by calling it rather than by reading app.routes: included routers
    are nested, so introspecting the route table is both fiddly and beside the
    point. What matters is that a request reaches the handler.
    """
    import os

    from fastapi.testclient import TestClient

    os.environ.setdefault("POND_ACCESS_KEY", "test-key")
    from app.config import settings

    settings.pond_access_key = "test-key"
    from app.main import app

    if not manifest()["capabilities"]["async_tasks"]:
        return

    r = TestClient(app).get(
        "/tasks/probe",
        headers={
            "Authorization": "Bearer test-key",
            "X-Agent-Protocol-Version": "1.0",
        },
    )
    # 404 task_not_found is the handler answering, not a missing route.
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "task_not_found"


def test_every_value_schema_carries_a_description():
    """valueSchema requires one, including on array item schemas, which is easy
    to miss because the outer property has its own."""

    def walk(node, where):
        if not isinstance(node, dict):
            return
        if "type" in node and where:
            assert node.get("description"), f"missing description at {where}"
        for key in ("properties",):
            for name, child in (node.get(key) or {}).items():
                walk(child, f"{where}/{name}")
        if isinstance(node.get("items"), dict):
            walk(node["items"], f"{where}/items")

    for action in manifest()["actions"]:
        for name, prop in action["input_schema"]["properties"].items():
            walk(prop, f"{action['id']}/{name}")


def test_manifest_stays_within_the_size_cap():
    assert len(json.dumps(manifest()).encode()) <= 256 * 1024
