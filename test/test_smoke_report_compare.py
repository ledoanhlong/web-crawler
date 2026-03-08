"""Tests for smoke report trend comparison helpers."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_module():
    script_path = Path("scripts/compare_smoke_reports.py")
    module_name = "compare_smoke_reports"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_compare_reports_detects_pass_rate_regression(tmp_path: Path) -> None:
    mod = _load_module()

    prev = {
        "summary": {"total": 2, "passed": 2, "failed": 0},
        "results": [
            {"profile_name": "a", "passed": True, "diagnostics": {"counters": {"empty_pages": 0, "method_switches": 0}, "parser_metrics": {"scalar_ratio": 0.9, "structured_ratio": 0.7}}},
            {"profile_name": "b", "passed": True, "diagnostics": {"counters": {"empty_pages": 1, "method_switches": 1}, "parser_metrics": {"scalar_ratio": 0.8, "structured_ratio": 0.6}}},
        ],
    }
    cur = {
        "summary": {"total": 2, "passed": 1, "failed": 1},
        "results": [
            {"profile_name": "a", "passed": False, "diagnostics": {"counters": {"empty_pages": 4, "method_switches": 2}, "parser_metrics": {"scalar_ratio": 0.5, "structured_ratio": 0.3}}},
            {"profile_name": "b", "passed": True, "diagnostics": {"counters": {"empty_pages": 2, "method_switches": 1}, "parser_metrics": {"scalar_ratio": 0.4, "structured_ratio": 0.2}}},
        ],
    }

    prev_path = tmp_path / "smoke-20260101-000000.json"
    cur_path = tmp_path / "smoke-20260102-000000.json"
    prev_path.write_text(json.dumps(prev), encoding="utf-8")
    cur_path.write_text(json.dumps(cur), encoding="utf-8")

    previous = mod.summarize_report(prev_path)
    current = mod.summarize_report(cur_path)
    ok, issues = mod.compare_reports(
        previous,
        current,
        max_pass_rate_drop=0.1,
        max_empty_pages_increase=1.0,
        max_switches_increase=0.5,
        max_parser_scalar_drop=0.1,
        max_parser_structured_drop=0.1,
        allow_new_failures=0,
    )

    assert not ok
    assert issues


def test_compare_reports_allows_stable_or_better(tmp_path: Path) -> None:
    mod = _load_module()

    prev = {
        "summary": {"total": 2, "passed": 1, "failed": 1},
        "results": [
            {"profile_name": "a", "passed": False, "diagnostics": {"counters": {"empty_pages": 3, "method_switches": 2}, "parser_metrics": {"scalar_ratio": 0.4, "structured_ratio": 0.2}}},
            {"profile_name": "b", "passed": True, "diagnostics": {"counters": {"empty_pages": 1, "method_switches": 1}, "parser_metrics": {"scalar_ratio": 0.5, "structured_ratio": 0.3}}},
        ],
    }
    cur = {
        "summary": {"total": 2, "passed": 2, "failed": 0},
        "results": [
            {"profile_name": "a", "passed": True, "diagnostics": {"counters": {"empty_pages": 1, "method_switches": 1}, "parser_metrics": {"scalar_ratio": 0.7, "structured_ratio": 0.5}}},
            {"profile_name": "b", "passed": True, "diagnostics": {"counters": {"empty_pages": 0, "method_switches": 0}, "parser_metrics": {"scalar_ratio": 0.8, "structured_ratio": 0.6}}},
        ],
    }

    prev_path = tmp_path / "smoke-20260101-000000.json"
    cur_path = tmp_path / "smoke-20260102-000000.json"
    prev_path.write_text(json.dumps(prev), encoding="utf-8")
    cur_path.write_text(json.dumps(cur), encoding="utf-8")

    previous = mod.summarize_report(prev_path)
    current = mod.summarize_report(cur_path)
    ok, issues = mod.compare_reports(
        previous,
        current,
        max_pass_rate_drop=0.1,
        max_empty_pages_increase=1.0,
        max_switches_increase=0.5,
        max_parser_scalar_drop=0.1,
        max_parser_structured_drop=0.1,
        allow_new_failures=0,
    )

    assert ok
    assert issues == []
