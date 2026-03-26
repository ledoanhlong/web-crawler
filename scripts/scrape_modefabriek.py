"""Standalone scraper for Modefabriek brand list.

Detail pages require login, so we extract only what's publicly available:
brand names and profile URLs from the listing page.

Usage:
    python scripts/scrape_modefabriek.py

Outputs CSV to output/modefabriek_brands.csv
"""

import asyncio
import csv
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

URL = "https://www.modefabriek.nl/en/event/brand-list-plattegrond"
BASE = "https://www.modefabriek.nl"
OUTPUT_DIR = Path("output")
OUTPUT_FILE = OUTPUT_DIR / "modefabriek_brands.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CSV_FIELDS = ["name", "profile_url"]


async def scrape():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient() as client:
        print(f"Fetching {URL}...")
        resp = await client.get(URL, headers=HEADERS, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        html = resp.text
        print(f"  Status: {resp.status_code} ({len(html)} bytes)")

        soup = BeautifulSoup(html, "lxml")

        # Brand links point to /nl/b2b-marketplace/merk/ or /en/b2b-marketplace/brand/
        brands = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/b2b-marketplace/merk/" in href or "/b2b-marketplace/brand/" in href:
                name = a.get_text(strip=True)
                if not name or name in seen:
                    continue
                seen.add(name)
                full_url = urljoin(BASE, href)
                brands.append({"name": name, "profile_url": full_url})

        print(f"\nFound {len(brands)} unique brands.")

        if not brands:
            print("ERROR: No brands found. The page structure may have changed.")
            print("First 2000 chars of HTML:")
            print(html[:2000])
            return

        # Write CSV
        print(f"Writing to {OUTPUT_FILE}...")
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(brands)

        # Print sample
        print(f"\nFirst 10 brands:")
        for b in brands[:10]:
            print(f"  {b['name']}")

        print(f"\nDone! {len(brands)} brands saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    start = time.perf_counter()
    asyncio.run(scrape())
    elapsed = time.perf_counter() - start
    print(f"Completed in {elapsed:.1f}s")
