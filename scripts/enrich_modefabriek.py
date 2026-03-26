"""Enrich Modefabriek brand list with LinkedIn, parent group, vertical, size, competitors.

Approach:
  1. Search DuckDuckGo for each brand's LinkedIn page + context
  2. Batch brands and send to Azure OpenAI (GPT-5.4) for structured classification
  3. Merge results and output enriched CSV

Usage:
    python scripts/enrich_modefabriek.py

Outputs CSV to output/modefabriek_enriched.csv
"""

import asyncio
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from ddgs import DDGS
from openai import AsyncAzureOpenAI

load_dotenv()

INPUT_FILE = Path("output/modefabriek_brands.csv")
OUTPUT_FILE = Path("output/modefabriek_enriched.csv")
PROGRESS_FILE = Path("output/_enrich_progress.json")

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Azure OpenAI
client = AsyncAzureOpenAI(
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
)
DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4")

CSV_FIELDS = [
    "brand_name",
    "company_name",
    "linkedin_url",
    "parent_group",
    "vertical",
    "sub_vertical",
    "organisation_size",
    "competitors_note",
]

BATCH_SIZE = 15  # brands per LLM call
SEARCH_DELAY = 1.5  # seconds between DuckDuckGo searches


def load_brands() -> list[dict]:
    brands = []
    with open(INPUT_FILE, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            brands.append({"brand_name": row["name"], "profile_url": row.get("profile_url", "")})
    return brands


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return {}


def save_progress(data: dict):
    PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def search_linkedin(brand_name: str) -> str:
    """Search DuckDuckGo for the brand's LinkedIn company page."""
    try:
        query = f'"{brand_name}" fashion LinkedIn company'
        results = DDGS().text(query, max_results=5)

        for r in results:
            href = r.get("href", "")
            if re.search(r"linkedin\.com/company/", href, re.IGNORECASE):
                return href

        # Fallback: check body text for LinkedIn URLs
        for r in results:
            body = r.get("body", "")
            match = re.search(r"https?://[\w.]*linkedin\.com/company/[\w-]+", body, re.IGNORECASE)
            if match:
                return match.group(0)

    except Exception as exc:
        print(f"    Search error for {brand_name}: {exc}")

    return ""


def search_brand_context(brand_name: str) -> str:
    """Search DuckDuckGo for general brand info to help LLM classification."""
    try:
        query = f'"{brand_name}" fashion brand company about'
        results = DDGS().text(query, max_results=3)

        snippets = []
        for r in results:
            title = r.get("title", "")
            body = r.get("body", "")
            if body:
                snippets.append(f"{title}: {body}")

        return " | ".join(snippets)[:600]
    except Exception:
        return ""


SYSTEM_PROMPT = """You are a B2B sales intelligence analyst specializing in the fashion industry.
You will receive a batch of fashion brand names with web search context snippets.
For each brand, provide structured classification data.

Rules:
- "vertical" should be a broad industry category (e.g., Fashion & Apparel, Accessories, Footwear, Lifestyle)
- "sub_vertical" should be more specific (e.g., Women's Ready-to-Wear, Denim, Outerwear, Bags & Leather Goods, Sustainable Fashion, Knitwear, Loungewear, Jewelry)
- "parent_group" — only fill if the brand is owned by a known larger company/group (e.g., PVH, LVMH, Bestseller, DK Company). Leave empty string if independent or unknown.
- "organisation_size" — classify as "Mid-Market" (roughly 50-1000 employees or €10M-€1B revenue) or "Enterprise" (1000+ employees or €1B+ revenue). If clearly a small/startup brand, put "SMB". If unknown, put "Unknown".
- "competitors_note" — if you know the brand works with or is distributed alongside notable competitors or if it's known to work with a competitor platform/retailer, note it briefly. Otherwise empty string.
- "company_name" — the official company/legal entity name if different from brand name. Otherwise same as brand name.

Respond ONLY with a JSON array. Each element must have exactly these keys:
brand_name, company_name, parent_group, vertical, sub_vertical, organisation_size, competitors_note

Do NOT include LinkedIn URLs — those are handled separately."""


async def classify_batch(batch: list[dict]) -> list[dict]:
    """Send a batch of brands to Azure OpenAI for classification."""
    brand_descriptions = []
    for b in batch:
        line = f"- {b['brand_name']}"
        ctx = b.get("_search_context", "")
        if ctx:
            line += f" (context: {ctx})"
        brand_descriptions.append(line)

    user_msg = (
        "Classify these fashion brands from the Modefabriek trade fair (Netherlands):\n\n"
        + "\n".join(brand_descriptions)
        + "\n\nRespond with a JSON array only."
    )

    for attempt in range(3):
        try:
            resp = await client.chat.completions.create(
                model=DEPLOYMENT,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.2,
                max_completion_tokens=4000,
            )
            text = resp.choices[0].message.content.strip()
            # Extract JSON from possible markdown code block
            json_match = re.search(r"\[.*\]", text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
            return json.loads(text)
        except Exception as exc:
            if attempt < 2:
                wait = 2 ** (attempt + 1)
                print(f"    LLM retry {attempt + 1} ({exc}), waiting {wait}s...")
                await asyncio.sleep(wait)
            else:
                print(f"    LLM FAILED for batch: {exc}")
                return [
                    {"brand_name": b["brand_name"], "company_name": b["brand_name"],
                     "parent_group": "", "vertical": "Fashion & Apparel",
                     "sub_vertical": "", "organisation_size": "Unknown",
                     "competitors_note": ""}
                    for b in batch
                ]


async def main():
    brands = load_brands()
    print(f"Loaded {len(brands)} brands from {INPUT_FILE}\n")

    progress = load_progress()
    linkedin_cache: dict[str, str] = progress.get("linkedin", {})
    context_cache: dict[str, str] = progress.get("context", {})

    # ── Phase 1: LinkedIn search + context gathering ──────────────────
    print("Phase 1: Searching for LinkedIn URLs and brand context...\n")
    new_searches = 0
    for i, brand in enumerate(brands):
        name = brand["brand_name"]

        if name in linkedin_cache:
            brand["linkedin_url"] = linkedin_cache[name]
            brand["_search_context"] = context_cache.get(name, "")
            continue

        print(f"  [{i + 1}/{len(brands)}] Searching: {name}")
        new_searches += 1

        # Search LinkedIn
        linkedin_url = search_linkedin(name)
        brand["linkedin_url"] = linkedin_url
        linkedin_cache[name] = linkedin_url

        time.sleep(SEARCH_DELAY)

        # Search general context
        context = search_brand_context(name)
        brand["_search_context"] = context
        context_cache[name] = context

        if linkedin_url:
            print(f"           LinkedIn: {linkedin_url}")
        else:
            print(f"           LinkedIn: not found")

        time.sleep(SEARCH_DELAY)

        # Save progress every 10 brands
        if new_searches % 10 == 0:
            save_progress({"linkedin": linkedin_cache, "context": context_cache})
            print(f"    (progress saved: {i + 1}/{len(brands)})\n")

    save_progress({"linkedin": linkedin_cache, "context": context_cache})
    found = sum(1 for b in brands if b.get("linkedin_url"))
    print(f"\nLinkedIn found for {found}/{len(brands)} brands.\n")

    # ── Phase 2: LLM classification in batches ────────────────────────
    print(f"Phase 2: Classifying brands via Azure OpenAI ({DEPLOYMENT})...\n")
    all_classifications = {}

    for batch_start in range(0, len(brands), BATCH_SIZE):
        batch = brands[batch_start:batch_start + BATCH_SIZE]
        batch_end = min(batch_start + BATCH_SIZE, len(brands))
        print(f"  Batch {batch_start + 1}-{batch_end} of {len(brands)}...")

        results = await classify_batch(batch)
        for item in results:
            # Normalize key for matching (brands are uppercase in CSV)
            key = item.get("brand_name", "").upper().strip()
            all_classifications[key] = item

        await asyncio.sleep(1)

    # ── Phase 3: Merge and write CSV ──────────────────────────────────
    print(f"\nPhase 3: Writing enriched CSV...\n")
    enriched = []
    for brand in brands:
        name = brand["brand_name"]
        classification = all_classifications.get(name.upper().strip(), {})

        row = {
            "brand_name": name,
            "company_name": classification.get("company_name", name),
            "linkedin_url": brand.get("linkedin_url", ""),
            "parent_group": classification.get("parent_group", ""),
            "vertical": classification.get("vertical", "Fashion & Apparel"),
            "sub_vertical": classification.get("sub_vertical", ""),
            "organisation_size": classification.get("organisation_size", "Unknown"),
            "competitors_note": classification.get("competitors_note", ""),
        }
        enriched.append(row)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(enriched)

    # Summary
    sizes = {}
    for r in enriched:
        s = r["organisation_size"]
        sizes[s] = sizes.get(s, 0) + 1

    print(f"Done! {len(enriched)} brands written to {OUTPUT_FILE}")
    print(f"\nSize breakdown:")
    for size, count in sorted(sizes.items()):
        print(f"  {size}: {count}")

    linkedin_count = sum(1 for r in enriched if r["linkedin_url"])
    group_count = sum(1 for r in enriched if r["parent_group"])
    print(f"\nLinkedIn found: {linkedin_count}/{len(enriched)}")
    print(f"Part of larger group: {group_count}/{len(enriched)}")

    # Cleanup progress file
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()


if __name__ == "__main__":
    start = time.perf_counter()
    asyncio.run(main())
    elapsed = time.perf_counter() - start
    print(f"\nCompleted in {elapsed:.1f}s")
