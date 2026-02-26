"""OutputAgent — Constructs the final structured output in JSON and CSV.

Also uses GPT for a final quality-check pass: deduplication, consistency
fixes, and enrichment of records that look incomplete.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from app.config import settings
from app.models.schemas import CrawlResult, ExhibitorRecord
from app.prompts import load_prompt
from app.utils.llm import chat_completion_json
from app.utils.logging import get_logger

log = get_logger(__name__)

# If there are fewer records than this, skip the LLM QA pass to save cost
_QA_THRESHOLD = 3


class OutputAgent:
    """Produce final JSON + CSV output files."""

    async def build_output(
        self,
        records: list[ExhibitorRecord],
        job_id: str,
    ) -> CrawlResult:
        log.info("Building output for %d records (job %s)", len(records), job_id)

        # --- Optional LLM quality pass ---
        if len(records) >= _QA_THRESHOLD:
            records = await self._quality_pass(records)

        # --- Prepare output directory ---
        out_dir = Path(settings.output_dir) / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        # --- JSON ---
        json_path = out_dir / "exhibitors.json"
        json_data = [r.model_dump(mode="json", exclude_none=True) for r in records]
        json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Wrote %s (%d records)", json_path, len(json_data))

        # --- CSV ---
        csv_path = out_dir / "exhibitors.csv"
        flat_records = self._flatten_for_csv(records)
        df = pd.DataFrame(flat_records)
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        log.info("Wrote %s (%d rows)", csv_path, len(df))

        return CrawlResult(
            records=records,
            json_path=str(json_path),
            csv_path=str(csv_path),
        )

    async def _quality_pass(self, records: list[ExhibitorRecord]) -> list[ExhibitorRecord]:
        """Send records through GPT for dedup + consistency cleanup."""
        log.info("Running LLM quality pass on %d records", len(records))
        serialized = [r.model_dump(mode="json", exclude_none=True) for r in records]

        # Process in chunks if there are many records
        chunk_size = 80
        cleaned: list[ExhibitorRecord] = []
        for i in range(0, len(serialized), chunk_size):
            chunk = serialized[i : i + chunk_size]
            messages = [
                {"role": "system", "content": load_prompt("output_qa")},
                {
                    "role": "user",
                    "content": (
                        f"Clean and deduplicate these {len(chunk)} records:\n"
                        f"```json\n{json.dumps(chunk, ensure_ascii=False)}\n```"
                    ),
                },
            ]
            result = await chat_completion_json(messages, max_tokens=16_000)
            for raw in result.get("records", []):
                try:
                    cleaned.append(ExhibitorRecord.model_validate(raw))
                except Exception as exc:
                    log.warning("QA pass: invalid record skipped: %s", exc)

        log.info("Quality pass: %d → %d records", len(records), len(cleaned))
        return cleaned if cleaned else records  # fallback to originals if LLM fails

    @staticmethod
    def _flatten_for_csv(records: list[ExhibitorRecord]) -> list[dict[str, str]]:
        """Flatten nested fields for a CSV-friendly representation."""
        rows: list[dict[str, str]] = []
        for r in records:
            row: dict[str, str] = {
                "name": r.name,
                "booth_or_stand": r.booth_or_stand or "",
                "country": r.country or "",
                "city": r.city or "",
                "address": r.address or "",
                "postal_code": r.postal_code or "",
                "website": r.website or "",
                "email": r.email or "",
                "phone": r.phone or "",
                "fax": r.fax or "",
                "description": r.description or "",
                "product_categories": "; ".join(r.product_categories),
                "brands": "; ".join(r.brands),
                "hall": r.hall or "",
                "logo_url": r.logo_url or "",
                "source_url": r.source_url or "",
            }
            # Add social media as separate columns
            for platform, url in r.social_media.items():
                row[f"social_{platform}"] = url
            # Add raw_extra fields
            for key, val in r.raw_extra.items():
                row[f"extra_{key}"] = val
            rows.append(row)
        return rows
