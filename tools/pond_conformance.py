"""Drive a running Foxy exactly as Pond does, and report what happens.

The first submission was rejected on four runtime points and one precision
point. Every one of them was reachable by making these calls, and I had never
made them - the agent was tested through Slack instead. So this exists to be
run against a real deployment before every submission.

    python tools/pond_conformance.py --base https://tryfoxy.vercel.app --key KEY

It checks what the reviewer checked:

  * the manifest is served and matches the published schema
  * every synchronous action returns a valid Pond result
  * the scan is accepted, pollable, and actually finishes
  * a requested source scope is honoured
  * a repeated idempotency key replays instead of re-running
  * bad input is refused with a field-level error
  * early signals are founder announcements

Exits non-zero if anything fails, so it can gate a submission.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent

PASS, FAIL = "pass", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((PASS if ok else FAIL, name, detail))
    mark = "  ok  " if ok else "  FAIL"
    print(f"{mark}  {name}" + (f"  -  {detail}" if detail else ""))
    return ok


def call(base: str, key: str, path: str, body: dict | None = None, **headers):
    h = {
        "Authorization": f"Bearer {key}",
        "X-Agent-Protocol-Version": "1.0",
        "Content-Type": "application/json",
    }
    h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=h,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=310) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"raw": raw.decode("utf-8", "replace")[:400]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="e.g. https://tryfoxy.vercel.app")
    ap.add_argument("--key", required=True, help="the Pond Access Key")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print(f"\nDriving {base} as Pond would\n")

    # --- manifest ----------------------------------------------------------
    status, manifest = call(base, args.key, "/manifest")
    check("manifest is served", status == 200, f"HTTP {status}")

    schema_path = ROOT / "tests" / "data" / "pond-manifest-schema.json"
    if schema_path.exists():
        try:
            import jsonschema

            errs = sorted(
                jsonschema.Draft202012Validator(
                    json.loads(schema_path.read_text(encoding="utf-8"))
                ).iter_errors(manifest),
                key=lambda e: list(e.path),
            )
            check(
                "manifest matches the published schema",
                not errs,
                "" if not errs else f"{len(errs)} error(s): {errs[0].message[:110]}",
            )
        except ImportError:
            print("       (jsonschema not installed, skipping schema check)")

    actions = [a["id"] for a in (manifest.get("actions") or [])]
    check("manifest declares actions", bool(actions), ", ".join(actions))

    # --- synchronous actions ----------------------------------------------
    sync = {
        "lookup_company": {"company_name": "Stripe"},
        "search_early_signals": {"limit": 5},
        "recent_detections": {"limit": 5},
        "health_check": {},
    }
    for action, params in sync.items():
        if action not in actions:
            continue
        t0 = time.time()
        status, body = call(
            base, args.key, "/runs",
            {"action_id": action, "parameters": params, "run_id": f"conf-{action}"},
        )
        took = time.time() - t0
        ok = status == 200 and body.get("status") == "completed" and body.get("output")
        check(f"{action} returns a Pond result", ok, f"HTTP {status}, {took:.1f}s")

        if action == "search_early_signals" and ok:
            _check_precision(body)

    # --- schema enforcement ------------------------------------------------
    bad = [
        ("unknown parameter", {"sourses": ["yc_directory"]}),
        ("value outside the enum", {"sources": ["crunchbase"]}),
        ("wrong type", {"post_to_slack": "yes"}),
    ]
    for label, params in bad:
        status, body = call(
            base, args.key, "/runs",
            {"action_id": "scan_now", "parameters": params, "run_id": f"bad-{label}"},
        )
        named = (body.get("error") or {}).get("details", {}).get("field")
        check(
            f"{label} is refused",
            status == 422 and bool(named),
            f"HTTP {status}, field={named}",
        )

    # --- idempotency -------------------------------------------------------
    body1 = {"action_id": "health_check", "parameters": {}, "run_id": "conf-idem"}
    _, first = call(base, args.key, "/runs", body1, **{"Idempotency-Key": "conf-idem"})
    _, second = call(base, args.key, "/runs", body1, **{"Idempotency-Key": "conf-idem"})
    check("a repeated idempotency key replays", first == second)

    # --- the scan ----------------------------------------------------------
    t0 = time.time()
    status, accepted = call(
        base, args.key, "/runs",
        {
            "action_id": "scan_now",
            "parameters": {"sources": ["yc_directory", "yc_launches"],
                           "post_to_slack": False},
            "run_id": "conf-scan",
        },
    )
    ok = status == 202 and accepted.get("task_id")
    check("scan is accepted as a task", ok, f"HTTP {status}")
    if not ok:
        return _summary()

    task_id = accepted["task_id"]
    final = {}
    for _ in range(60):
        time.sleep(max(1, accepted.get("poll_after_ms", 3000) / 1000))
        status, final = call(base, args.key, f"/tasks/{task_id}")
        if final.get("status") not in {"queued", "running"}:
            break
    took = time.time() - t0

    check(
        "scan completes",
        final.get("status") == "completed",
        f"{final.get('status')} after {took:.0f}s"
        + (f" - {(final.get('error') or {}).get('message', '')}" if final.get("status") == "failed" else ""),
    )

    if final.get("status") == "completed":
        text = (final.get("output") or [{}])[0].get("text", "")
        scanned = {
            line.split("**")[1]
            for line in text.splitlines()
            if line.startswith("- **") and " · " in line
        }
        check(
            "scan honours the requested sources",
            scanned <= {"yc_directory", "yc_launches"} and scanned,
            f"scanned {sorted(scanned)}",
        )

    return _summary()


def _check_precision(body: dict) -> None:
    """Every early signal must look like a person announcing."""
    text = "".join(part.get("text", "") for part in body.get("output") or [])
    lines = [ln for ln in text.splitlines() if ln.startswith("- **")]
    if not lines:
        check("early signals are founder announcements", True, "none recorded")
        return
    bad = [ln for ln in lines if "linkedin.com/company/" in ln.lower()]
    check(
        "early signals are founder announcements",
        not bad,
        f"{len(lines)} listed, {len(bad)} are company pages",
    )


def _summary() -> int:
    failed = [r for r in _results if r[0] == FAIL]
    print(f"\n  {len(_results) - len(failed)}/{len(_results)} checks passed")
    for _, name, detail in failed:
        print(f"    FAILED  {name}  {detail}")
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
