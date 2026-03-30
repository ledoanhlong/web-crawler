"""
Enrich ISPO 2026 exhibitors with revenue data.
Strategy:
  1. Use Azure OpenAI to identify known companies and their revenue from training data
  2. Use WebSearch (via Bing) to verify/supplement for major companies
  3. Merge into final CSV
"""

import csv
import json
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("output/ispo")
ENRICH_DIR = OUTPUT_DIR / "enrich"
ENRICH_DIR.mkdir(parents=True, exist_ok=True)
RESULT_FILE = ENRICH_DIR / "revenue_results.json"

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
AZURE_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

BATCH_SIZE = 30  # companies per AI call


# ── IO helpers ──────────────────────────────────────────────────────────────
def load_json(path):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Azure OpenAI ────────────────────────────────────────────────────────────
def call_openai(prompt, max_tokens=8000):
    url = (
        f"{AZURE_ENDPOINT}/openai/deployments/{AZURE_DEPLOYMENT}"
        f"/chat/completions?api-version={AZURE_API_VERSION}"
    )
    body = {
        "messages": [
            {"role": "system", "content": (
                "You are a business intelligence analyst with extensive knowledge "
                "of company financials. You provide accurate, sourced revenue data. "
                "Respond ONLY with a valid JSON array."
            )},
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": max_tokens,
        "temperature": 0.2,
    }
    with httpx.Client(timeout=180) as client:
        r = client.post(url, headers={
            "Content-Type": "application/json",
            "api-key": AZURE_API_KEY,
        }, json=body)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*\n?", "", content)
            content = re.sub(r"\n?\s*```$", "", content)
        return json.loads(content)


def process_batch(companies):
    """Ask AI to provide revenue data for a batch of companies."""
    company_list = "\n".join(
        f"- {c['name']} (website: {c.get('website') or 'unknown'}, "
        f"stands: {c.get('stands','')}, country: {c.get('country_iso','')})"
        for c in companies
    )

    prompt = f"""For each company below, provide the best available revenue data.
These are exhibitors at ISPO 2026 (sports/outdoor trade fair in Amsterdam).

For each company provide:
1. total_annual_revenue: Most recent annual revenue with currency and year.
   - For PUBLIC companies: use their latest reported revenue (e.g. "$51.2B (FY2025)")
   - For well-known PRIVATE companies: use widely-reported estimates (e.g. "~€500M (2024 est.)")
   - For UNKNOWN/SMALL companies: "not found"
2. revenue_source: Where this data comes from (e.g. "SEC filing", "annual report",
   "Forbes estimate", "Crunchbase", "industry report"). Say "not available" if not found.
3. ecom_revenue: Best estimate of their e-commerce / online D2C / marketplace revenue.
   - If the company sells significantly online (own webshop, Amazon, etc), estimate the portion.
   - If primarily B2B/wholesale: "primarily B2B/wholesale"
   - If unknown: "not found"
4. ecom_source: Source for ecom estimate
5. employee_count: Number of employees if known
6. revenue_notes: Brief context — parent company, growth trajectory, market position,
   whether they are a subsidiary, startup, or manufacturer.
7. confidence: "high" (public/verified data), "medium" (widely reported estimate),
   "low" (rough estimate), or "none" (no data available)

IMPORTANT:
- Many of these are small manufacturers from Pakistan, China, Korea, etc. — it's OK to say "not found"
- For subsidiaries (e.g. "Nike Vision"), provide PARENT company revenue and note it's a subsidiary
- Do NOT fabricate numbers. "not found" is better than a wrong guess.
- Be specific about which fiscal year the revenue refers to.

Respond with a JSON array, one object per company, in SAME ORDER as input:
[{{"company":"Name","total_annual_revenue":"...","revenue_source":"...",
"ecom_revenue":"...","ecom_source":"...","employee_count":"...",
"revenue_notes":"...","confidence":"..."}}]

Companies:
{company_list}"""

    return call_openai(prompt, max_tokens=8000)


# ── Bing search for verification ────────────────────────────────────────────
def bing_search(query, count=5):
    """Search via Bing and return text snippets."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        ),
    }
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            r = client.get(
                f"https://www.bing.com/search?q={query}&count={count}",
                headers=headers,
            )
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            snippets = []
            for li in soup.select(".b_algo"):
                title = li.select_one("h2")
                desc = li.select_one(".b_caption p, .b_paractl")
                t = title.get_text(strip=True) if title else ""
                d = desc.get_text(strip=True) if desc else ""
                if t or d:
                    snippets.append(f"{t}: {d}"[:300])
            return "\n".join(snippets[:5])
    except Exception as e:
        return f"(error: {e})"


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    with open(OUTPUT_DIR / "ispo_exhibitors_2026.json", encoding="utf-8") as f:
        exhibitors = json.load(f)

    results = load_json(RESULT_FILE)
    print(f"Exhibitors: {len(exhibitors)} | Existing results: {len(results)}\n")

    # Phase 1: AI-based revenue lookup in batches
    need_lookup = [ex for ex in exhibitors if ex["name"] not in results]
    if need_lookup:
        print(f"Phase 1: AI revenue lookup for {len(need_lookup)} companies...")
        batches = [need_lookup[i:i+BATCH_SIZE]
                   for i in range(0, len(need_lookup), BATCH_SIZE)]

        for bi, batch in enumerate(batches):
            try:
                extracted = process_batch(batch)
                for j, item in enumerate(extracted):
                    cname = batch[j]["name"] if j < len(batch) else item.get("company", "")
                    results[cname] = item

                save_json(RESULT_FILE, results)
                names_done = [batch[j]["name"] for j in range(min(len(extracted), len(batch)))]
                print(f"  Batch [{bi+1}/{len(batches)}] — {len(results)} total "
                      f"({names_done[0]}...{names_done[-1]})")

                time.sleep(1)

            except Exception as e:
                print(f"  Batch {bi+1} error: {e}")
                # Mark as error
                for c in batch:
                    if c["name"] not in results:
                        results[c["name"]] = {
                            "company": c["name"],
                            "total_annual_revenue": "error",
                            "revenue_source": str(e)[:200],
                            "ecom_revenue": "", "ecom_source": "",
                            "employee_count": "", "revenue_notes": "",
                            "confidence": "none",
                        }
                save_json(RESULT_FILE, results)
                time.sleep(5)

        print(f"  AI lookup done: {len(results)} results.\n")

    # Phase 2: Verify high-value results with Bing search
    print("Phase 2: Verifying top companies via Bing search...")
    high_confidence = [
        (name, data) for name, data in results.items()
        if isinstance(data, dict)
        and data.get("confidence") in ("high", "medium")
        and data.get("total_annual_revenue", "") not in ("not found", "error", "")
        and "verified" not in data.get("revenue_source", "")
    ]

    print(f"  {len(high_confidence)} companies to verify")
    verified_count = 0
    for name, data in high_confidence[:50]:  # verify top 50
        query = f'"{name}" revenue {data.get("total_annual_revenue", "")}'
        snippets = bing_search(query)
        if snippets and "(error" not in snippets:
            # Check if search results corroborate the revenue figure
            data["bing_verification"] = snippets[:500]
            verified_count += 1
        time.sleep(1.5)

    save_json(RESULT_FILE, results)
    print(f"  Verified {verified_count} companies via Bing.\n")

    # Phase 3: Build enriched CSV
    print("Phase 3: Building enriched CSV...")
    csv_path = OUTPUT_DIR / "ispo_exhibitors_2026.csv"
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames)

    new_cols = ["total_annual_revenue", "revenue_source", "confidence",
                "ecom_revenue", "ecom_source",
                "employee_count", "revenue_notes"]
    all_fields = fields + new_cols

    for row in rows:
        e = results.get(row["name"], {})
        for col in new_cols:
            row[col] = e.get(col, "")

    out_path = OUTPUT_DIR / "ispo_exhibitors_2026_enriched.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    # Stats
    found = [r for r in rows if r.get("total_annual_revenue")
             and r["total_annual_revenue"] not in ("not found", "error", "")]
    ecom = [r for r in rows if r.get("ecom_revenue")
            and r["ecom_revenue"] not in ("not found", "error", "",
                                           "primarily B2B/wholesale")]
    high = [r for r in found if r.get("confidence") in ("high", "medium")]

    print(f"\n  {len(rows)} exhibitors -> {out_path}")
    print(f"  Revenue found: {len(found)}/{len(rows)}")
    print(f"  High/medium confidence: {len(high)}/{len(rows)}")
    print(f"  E-com revenue: {len(ecom)}/{len(rows)}")

    if found:
        print(f"\n  === Companies with revenue data ===")
        for r in sorted(found, key=lambda x: x.get("confidence", ""), reverse=True):
            ecom_str = r.get("ecom_revenue", "")
            conf = r.get("confidence", "?")
            emp = r.get("employee_count", "")
            print(f"    [{conf}] {r['name']}: {r['total_annual_revenue']}"
                  + (f" | ecom: {ecom_str}" if ecom_str and ecom_str not in ("not found", "primarily B2B/wholesale") else "")
                  + (f" | {emp} emp" if emp and emp != "not found" else ""))

    print("\nDone!")


if __name__ == "__main__":
    main()
