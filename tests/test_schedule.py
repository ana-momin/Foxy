"""The clock hosted mode runs on.

Foxy is meant to keep working long after anyone last opened this repository,
and the thing standing in the way is a GitHub rule: a scheduled workflow is
switched off after 60 days without a commit. Nothing in the application would
notice - the web app stays up, the console still answers, the sources still
look healthy, and no sweep ever runs again.

So the sweep pushes an empty commit when the repository has gone quiet. These
tests hold that arrangement in place, because losing it fails silently and only
becomes visible two months later.
"""

from __future__ import annotations

import pathlib
import re

# A declared dependency, so importing it outright rather than skipping on it:
# a test that can quietly turn itself off is no guard at all.
import yaml

SWEEP = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "hosted-sweep.yml"

# GitHub's limit is 60 days. Anything at or above this leaves too little room
# for a sweep to fail a few times in a row before the deadline.
LATEST_SAFE_INTERVAL = 55


def workflow() -> dict:
    return yaml.safe_load(SWEEP.read_text(encoding="utf-8"))


def keepalive() -> dict:
    steps = workflow()["jobs"]["sweep"]["steps"]
    found = [s for s in steps if "--allow-empty" in (s.get("run") or "")]
    assert found, "the sweep no longer keeps its own schedule alive"
    return found[0]


def test_the_sweep_is_still_scheduled():
    # `on` is truthy YAML: PyYAML reads the bare key as the boolean True.
    triggers = workflow().get("on") or workflow().get(True)
    assert triggers.get("schedule"), "hosted mode lost its clock"


def test_the_sweep_keeps_its_own_schedule_alive():
    step = keepalive()
    assert step.get("continue-on-error") is True, (
        "a failed keepalive must not fail a sweep that already did its work"
    )
    assert "schedule" in step.get("if", ""), (
        "the keepalive should answer to the cron, not to a manual run"
    )


def test_the_keepalive_fires_before_github_gives_up():
    # The age comparison, not any number in the step: the step also divides by
    # the seconds in a day, which is a fine number and a terrible threshold.
    threshold = re.search(r'-ge\s+"?(\d+)"?', keepalive()["run"])
    assert threshold, "the keepalive no longer compares the age of the repository"
    days = int(threshold.group(1))
    assert days <= LATEST_SAFE_INTERVAL, (
        f"the keepalive waits {days} days; GitHub stops the schedule at 60"
    )


def test_the_keepalive_can_actually_push():
    assert workflow()["jobs"]["sweep"].get("permissions", {}).get("contents") == "write", (
        "the job cannot push, so the keepalive would fail every time"
    )


def test_the_keepalive_does_not_start_a_test_run():
    assert "[skip ci]" in keepalive()["run"], (
        "an empty commit without [skip ci] mails the owner a CI run every time"
    )
