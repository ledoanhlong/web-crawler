"""Tests for smoke regression issue payload builder."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    script_path = Path("scripts/smoke_issue_payload.py")
    module_name = "smoke_issue_payload"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_build_issue_payload_includes_failed_profiles_and_summary() -> None:
    mod = _load_module()
    report = {
        "summary": {"total": 3, "passed": 1, "failed": 2},
        "results": [
            {"profile_name": "a", "passed": False},
            {"profile_name": "b", "passed": True},
            {"profile_name": "c", "passed": False},
        ],
    }

    payload = mod.build_issue_payload(report, summary_markdown="# Smoke Trend Summary\n\n- Failed: 2")

    assert "Smoke Regression" in payload["title"]
    assert "`a`" in payload["body"]
    assert "`c`" in payload["body"]
    assert "Embedded Smoke Summary" in payload["body"]


def test_build_issue_payload_handles_no_failed_profiles() -> None:
    mod = _load_module()
    report = {
        "summary": {"total": 1, "passed": 1, "failed": 0},
        "results": [{"profile_name": "ok", "passed": True}],
    }

    payload = mod.build_issue_payload(report)

    assert "0/1" in payload["title"]
    assert "Next Actions" in payload["body"]
