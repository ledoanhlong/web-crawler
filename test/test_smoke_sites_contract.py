"""Contract tests for smoke suite pass/fail evaluation rules."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    script_path = Path("scripts/smoke_sites.py")
    module_name = "smoke_sites"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_evaluate_pass_respects_min_records() -> None:
    mod = _load_module()

    final_job = {
        "status": "completed",
        "result": {"records": [{"name": "A"}]},
        "quality_report": {"overall_score": 0.9},
    }
    profile = {"expect_min_records": 2}

    ok, reason, count = mod._evaluate_pass(final_job=final_job, profile=profile)

    assert not ok
    assert "record_count" in reason
    assert count == 1


def test_evaluate_pass_respects_min_quality() -> None:
    mod = _load_module()

    final_job = {
        "status": "completed",
        "result": {"records": [{"name": "A"}, {"name": "B"}]},
        "quality_report": {"overall_score": 0.42},
    }
    profile = {"expect_min_records": 1, "expect_min_quality": 0.5}

    ok, reason, _ = mod._evaluate_pass(final_job=final_job, profile=profile)

    assert not ok
    assert "quality_score" in reason


def test_evaluate_pass_accepts_partial_when_allowed() -> None:
    mod = _load_module()

    final_job = {
        "status": "partial",
        "result": {"records": [{"name": "A"}]},
        "quality_report": {"overall_score": 0.8},
    }
    profile = {"allow_partial": True, "expect_min_records": 1}

    ok, reason, count = mod._evaluate_pass(final_job=final_job, profile=profile)

    assert ok
    assert reason == "ok"
    assert count == 1


def test_resolve_profiles_path_prefers_private(monkeypatch, tmp_path: Path) -> None:
    mod = _load_module()
    private_path = tmp_path / "website_profiles.private.json"
    example_path = tmp_path / "website_profiles.example.json"
    private_path.write_text("[]", encoding="utf-8")
    example_path.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(mod, "PROFILE_PRIVATE_PATH", str(private_path))
    monkeypatch.setattr(mod, "PROFILE_EXAMPLE_PATH", str(example_path))

    resolved = mod._resolve_profiles_path("auto")
    assert resolved == private_path


def test_resolve_profiles_path_falls_back_to_example(monkeypatch, tmp_path: Path) -> None:
    mod = _load_module()
    private_path = tmp_path / "website_profiles.private.json"
    example_path = tmp_path / "website_profiles.example.json"
    example_path.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(mod, "PROFILE_PRIVATE_PATH", str(private_path))
    monkeypatch.setattr(mod, "PROFILE_EXAMPLE_PATH", str(example_path))

    resolved = mod._resolve_profiles_path("auto")
    assert resolved == example_path


def test_resolve_profiles_path_respects_explicit_arg(tmp_path: Path) -> None:
    mod = _load_module()
    explicit = tmp_path / "my-profiles.json"

    resolved = mod._resolve_profiles_path(str(explicit))
    assert resolved == explicit
