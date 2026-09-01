"""Pond Protocol V1 conformance, checked against the published contract."""

import os

from fastapi.testclient import TestClient

os.environ.setdefault("POND_ACCESS_KEY", "test-key")

from app.config import settings  # noqa: E402

settings.pond_access_key = "test-key"

from app.main import app  # noqa: E402

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-key", "X-Agent-Protocol-Version": "1.0"}


def test_manifest_is_public_and_needs_no_version_header():
    """Per spec, /manifest must be readable without an Access Key or a
    protocol-version request header."""
    r = client.get("/manifest")
    assert r.status_code == 200
    m = r.json()
    assert m["protocol"] == "marketplace-agent"
    assert m["protocol_version"] == "1.0"
    for key in ("agent_version", "capabilities", "input_modes", "output_modes", "limits"):
        assert key in m


def test_manifest_stays_under_the_size_cap():
    assert len(client.get("/manifest").content) <= 256 * 1024


def test_manifest_declares_no_secret():
    body = client.get("/manifest").text
    assert "test-key" not in body


def test_every_action_declares_a_schema():
    for action in client.get("/manifest").json()["actions"]:
        assert action["id"] and action["name"] and action["description"]
        assert action["input_schema"]["type"] == "object"


def test_missing_version_header_is_invalid_request():
    r = client.post("/runs", headers={"Authorization": "Bearer test-key"}, json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_patch_version_is_malformed():
    h = dict(AUTH)
    h["X-Agent-Protocol-Version"] = "1.0.1"
    assert client.post("/runs", headers=h, json={}).json()["error"]["code"] == "invalid_request"


def test_unsupported_minor_version():
    h = dict(AUTH)
    h["X-Agent-Protocol-Version"] = "1.1"
    body = client.post("/runs", headers=h, json={}).json()
    assert body["error"]["code"] == "unsupported_protocol_version"


def test_bad_key_is_unauthorized():
    h = dict(AUTH)
    h["Authorization"] = "Bearer wrong"
    r = client.post("/runs", headers=h, json={})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_unknown_action_is_unsupported_operation():
    r = client.post("/runs", headers=AUTH, json={"run_id": "r", "action_id": "nope"})
    assert r.json()["error"]["code"] == "unsupported_operation"


def test_missing_required_input_is_invalid_input():
    r = client.post(
        "/runs",
        headers=AUTH,
        json={"run_id": "r", "action_id": "lookup_company", "parameters": {}},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_input"


def test_terminal_response_carries_cumulative_usage():
    """Pond records a metering error when usage is missing or malformed."""
    r = client.post(
        "/runs",
        headers=AUTH,
        json={"run_id": "r1", "action_id": "health_check", "parameters": {}},
    )
    body = r.json()
    assert body["status"] == "completed"
    assert body["run_id"] == "r1"
    assert body["usage"]["unit_of_measurement"] in {"token", "result", "other"}
    assert isinstance(body["usage"]["quantity"], int)
    assert body["usage"]["quantity"] >= 0


def test_repeated_run_id_returns_the_saved_result():
    """Pond sends Idempotency-Key == run_id and does not retry; a duplicate
    must return the stored result rather than executing twice."""
    payload = {"run_id": "dup", "action_id": "health_check", "parameters": {}}
    first = client.post("/runs", headers=AUTH, json=payload).json()
    second = client.post("/runs", headers=AUTH, json=payload).json()
    assert first == second


def test_healthz_is_public():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] in {"ok", "degraded"}


def test_landing_page_is_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def _asgi_get(path: str, headers=None):
    """Call the app with a hand-built scope.

    httpx parses "//manifest" as a protocol-relative URL, so a normal test
    client cannot express this path at all. A raw scope can.
    """
    hdrs = [(b"host", b"example.test")]
    for k, v in (headers or {}).items():
        hdrs.append((k.lower().encode(), v.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "https", "path": path,
        "raw_path": path.encode(), "query_string": b"", "headers": hdrs,
        "client": ("1.2.3.4", 1), "server": ("example.test", 443), "root_path": "",
    }
    captured = {"status": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    await app(scope, receive, send)
    return captured


def test_double_slash_paths_still_resolve():
    """Pond appends fixed paths to the Server Base URL. A base URL entered with
    a trailing slash produces "//manifest".

    Behind Vercel the edge answers that with a 308 before the function runs, so
    the real fix there is to enter the URL without a trailing slash. A
    self-hosted deployment has no such edge, and must answer it directly rather
    than redirecting, because Pond's validator does not follow redirects.
    """
    import asyncio
    import json

    for path in ("/manifest", "//manifest", "///manifest"):
        got = asyncio.run(_asgi_get(path))
        assert got["status"] == 200, f"{path} returned {got['status']}"
        assert json.loads(got["body"])["protocol"] == "marketplace-agent"

    assert asyncio.run(_asgi_get("//healthz"))["status"] == 200


def test_double_slash_tasks_reaches_the_handler():
    import asyncio
    import json

    got = asyncio.run(
        _asgi_get(
            "//tasks/probe",
            {"Authorization": "Bearer test-key", "X-Agent-Protocol-Version": "1.0"},
        )
    )
    assert got["status"] != 308
    assert json.loads(got["body"])["error"]["code"] == "task_not_found"


def test_head_is_accepted_wherever_get_is():
    """Starlette does not derive HEAD from a GET route, so a checker that
    probes with HEAD first saw 405 and reported the endpoint as missing."""
    for path in ("/manifest", "/healthz"):
        assert client.request("HEAD", path).status_code == 200, path


def test_options_is_answered_everywhere():
    for path in ("/manifest", "/runs", "/tasks/anything"):
        r = client.request("OPTIONS", path)
        assert r.status_code == 204, path
        assert "GET" in r.headers.get("Allow", "")


def test_manifest_is_readable_cross_origin():
    r = client.get("/manifest")
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
