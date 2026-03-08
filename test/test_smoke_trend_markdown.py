"""Tests for smoke trend markdown summary builder."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_module():
    script_path = Path("scripts/smoke_trend_markdown.py")
    module_name = "smoke_trend_markdown"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_build_markdown_includes_metrics_and_failures(tmp_path: Path) -> None:
    mod = _load_module()

    prev_payload = {
        "summary": {"total": 2, "passed": 2, "failed": 0},
        "results": [
            {"profile_name": "a", "passed": True, "diagnostics": {"counters": {"empty_pages": 0, "method_switches": 0}, "parser_metrics": {"scalar_ratio": 0.9, "structured_ratio": 0.7}}},
            {"profile_name": "b", "passed": True, "diagnostics": {"counters": {"empty_pages": 1, "method_switches": 1}, "parser_metrics": {"scalar_ratio": 0.8, "structured_ratio": 0.6}}},
        ],
    }
    cur_payload = {
        "summary": {"total": 2, "passed": 1, "failed": 1},
        "results": [
            {"profile_name": "a", "status": "failed", "reason": "bad selectors", "passed": False, "diagnostics": {"counters": {"empty_pages": 4, "method_switches": 2}, "parser_metrics": {"scalar_ratio": 0.5, "structured_ratio": 0.3}}},
            {"profile_name": "b", "status": "completed", "reason": "ok", "passed": True, "diagnostics": {"counters": {"empty_pages": 1, "method_switches": 1}, "parser_metrics": {"scalar_ratio": 0.7, "structured_ratio": 0.5}}},
        ],
    }

    prev_path = tmp_path / "smoke-20260101-000000.json"
    cur_path = tmp_path / "smoke-20260102-000000.json"
    prev_path.write_text(json.dumps(prev_payload), encoding="utf-8")
    cur_path.write_text(json.dumps(cur_payload), encoding="utf-8")

    md = mod.build_markdown(
        previous_path=prev_path,
        current_path=cur_path,
        previous_payload=prev_payload,
        current_payload=cur_payload,
    )

    assert "Smoke Trend Summary" in md
    assert "Delta vs Previous" in md
    assert "Failed Profiles" in md
    assert "bad selectors" in md
    assert "Hotspots" in md
    assert "Most Common Failure Reasons" in md
    assert "Highest Empty-Page Profiles" in md
    assert "Highest Method-Switch Profiles" in md
    assert "Avg parser scalar completeness" in md
    assert "Lowest Parser Scalar-Completeness Profiles" in md


def test_build_markdown_handles_no_failures(tmp_path: Path) -> None:
    mod = _load_module()

    cur_payload = {
        "summary": {"total": 1, "passed": 1, "failed": 0},
        "results": [
            {"profile_name": "a", "passed": True, "diagnostics": {"counters": {"empty_pages": 0, "method_switches": 0}, "parser_metrics": {"scalar_ratio": 0.8, "structured_ratio": 0.6}}},
        ],
    }
    cur_path = tmp_path / "smoke-20260103-000000.json"
    cur_path.write_text(json.dumps(cur_payload), encoding="utf-8")

    md = mod.build_markdown(
        previous_path=None,
        current_path=cur_path,
        previous_payload=None,
        current_payload=cur_payload,
    )

    assert "No failed profiles in current report." in md
    assert "No failure reasons to report." in md
