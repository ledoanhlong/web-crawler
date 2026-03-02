"""Sandboxed script executor for ScrapeGraphAI-generated Python scripts.

Executes generated scraping scripts in an isolated subprocess with:
- Clean environment (no Azure keys or app secrets)
- Hard timeout (default 60s)
- Output size limits (1 MB)
- Pre-execution safety scan for dangerous patterns
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from app.utils.logging import get_logger

log = get_logger(__name__)

# Patterns that should block execution
_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\bos\.system\s*\(", "os.system() call"),
    (r"\bsubprocess\b", "subprocess module usage"),
    (r"\b__import__\s*\(", "dynamic __import__() call"),
    (r"\beval\s*\(", "eval() call"),
    (r"\bexec\s*\(", "exec() call"),
    (r"\bshutil\.(rmtree|move|copy)", "shutil destructive operation"),
    (r"\bsocket\b", "raw socket usage"),
    (r"\bhttp\.server\b", "HTTP server creation"),
    (r"\bos\.remove\b|\bos\.unlink\b|\bos\.rmdir\b", "file/dir deletion"),
    (r"\bos\.environ\b", "environment variable access"),
]

MAX_OUTPUT_BYTES = 1_048_576  # 1 MB
DEFAULT_TIMEOUT_S = 60


def validate_script(script: str) -> list[str]:
    """Scan a script for dangerous patterns.

    Returns a list of human-readable warnings.  An empty list means the
    script passed validation.
    """
    warnings: list[str] = []
    for pattern, description in _DANGEROUS_PATTERNS:
        if re.search(pattern, script):
            warnings.append(f"Blocked: {description} detected in generated script")
    return warnings


def _build_safe_env() -> dict[str, str]:
    """Build a minimal environment for the subprocess — no app secrets."""
    safe = {}
    # Inherit only essential vars
    for key in ("PATH", "PYTHONPATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE"):
        val = os.environ.get(key)
        if val:
            safe[key] = val
    # Ensure Python can find installed packages
    if "PYTHONPATH" not in safe:
        # Add site-packages so beautifulsoup4/requests/etc. are importable
        site_pkgs = Path(sys.executable).parent / "Lib" / "site-packages"
        if site_pkgs.exists():
            safe["PYTHONPATH"] = str(site_pkgs)
    return safe


async def execute_script(
    script: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> dict:
    """Execute a Python script in a sandboxed subprocess.

    Returns a dict matching the ScriptExecutionResult schema:
        stdout, stderr, returncode, timed_out, safety_warnings
    """
    # Step 1: safety check
    warnings = validate_script(script)
    if warnings:
        log.warning("Script blocked by safety scan: %s", warnings)
        return {
            "stdout": "",
            "stderr": "",
            "returncode": -1,
            "timed_out": False,
            "safety_warnings": warnings,
        }

    # Step 2: write to temp dir and execute
    def _run() -> dict:
        with tempfile.TemporaryDirectory(prefix="scraper_exec_") as tmp_dir:
            script_path = Path(tmp_dir) / "script.py"
            script_path.write_text(script, encoding="utf-8")

            safe_env = _build_safe_env()
            timed_out = False

            try:
                proc = subprocess.run(
                    [sys.executable, "-u", str(script_path)],
                    capture_output=True,
                    timeout=timeout,
                    cwd=tmp_dir,
                    env=safe_env,
                )
                stdout = proc.stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
                stderr = proc.stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                stdout = ""
                stderr = f"Script execution timed out after {timeout} seconds."
                returncode = -1
                timed_out = True

            return {
                "stdout": stdout,
                "stderr": stderr,
                "returncode": returncode,
                "timed_out": timed_out,
                "safety_warnings": [],
            }

    log.info("Executing generated script (timeout=%ds)", timeout)
    result = await asyncio.to_thread(_run)
    log.info(
        "Script execution finished: returncode=%d, timed_out=%s, stdout=%d chars",
        result["returncode"],
        result["timed_out"],
        len(result["stdout"]),
    )
    return result
