"""Standalone scraper for IAW Messe exhibitor directory.

Usage:
    python scripts/scrape_iaw.py

Outputs CSV to output/iaw_exhibitors.csv
"""

import asyncio
import csv
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.iaw-messe.de/en/visitors/list-of-exhibitors/"
OUTPUT_DIR = Path("output")
OUTPUT_FILE = OUTPUT_DIR / "iaw_exhibitors.csv"
CONCURRENCY = 5
DELAY_S = 0.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CSV_FIELDS = [
    "name", "hall_stand", "street", "postal_code", "city", "country",
    "phone", "fax", "email", "website", "product_categories", "detail_url",
]


async def fetch(client: httpx.AsyncClient, url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            resp = await client.get(url, headers=HEADERS, follow_redirects=True, timeout=30)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"  Retry {attempt + 1} for {url} ({exc}), waiting {wait}s...")
                await asyncio.sleep(wait)
            else:
                print(f"  FAILED: {url} — {exc}")
                return None


def parse_listing_page(html: str) -> list[dict]:
    """Extract exhibitor name + detail link from a listing page."""
    soup = BeautifulSoup(html, "lxml")
    exhibitors = []

    # Find all links whose href contains av_detail
    for link in soup.find_all("a", href=re.compile(r"av_detail=")):
        name_tag = link.find("h4")
        if not name_tag:
            continue
        name = name_tag.get_text(strip=True)
        if not name:
            continue

        hall_stand = ""
        span = link.find("span")
        if span:
            hall_stand = span.get_text(strip=True)

        detail_url = urljoin(BASE_URL, link["href"])
        exhibitors.append({
            "name": name,
            "hall_stand": hall_stand,
            "detail_url": detail_url,
        })

    return exhibitors


def parse_detail_page(html: str) -> dict:
    """Extract contact details from an exhibitor detail page."""
    soup = BeautifulSoup(html, "lxml")
    data: dict[str, str] = {}

    # Company name from h1
    h1 = soup.find("h1")
    if h1:
        data["name"] = h1.get_text(strip=True)

    # Hall/stand from h3 near the top
    for h3 in soup.find_all("h3"):
        text = h3.get_text(strip=True)
        if re.search(r"(Hall|Stand|Halle)", text, re.IGNORECASE):
            data["hall_stand"] = text
            break

    # Look for the contact section — usually after an h4 "Contact" or similar
    # Parse all paragraphs for address, phone, web, fax, email
    body_text = soup.get_text("\n", strip=True)

    # Phone
    phone_match = re.search(r"(?:Phone|Tel|Telefon)[:\s]*([+\d\s\-/()]+)", body_text, re.IGNORECASE)
    if phone_match:
        data["phone"] = phone_match.group(1).strip()

    # Fax
    fax_match = re.search(r"Fax[:\s]*([+\d\s\-/()]+)", body_text, re.IGNORECASE)
    if fax_match:
        data["fax"] = fax_match.group(1).strip()

    # Email
    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", body_text)
    if email_match:
        data["email"] = email_match.group(0)

    # Website — look for anchor tags with external hrefs
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and "iaw-messe.de" not in href and "wp-admin" not in href:
            data["website"] = href
            break

    # Address — look for the text block between "Contact" header and the phone/web lines
    # Try to find paragraphs in the contact area
    address_parts = _extract_address(soup)
    if address_parts:
        data.update(address_parts)

    # Categories
    categories = _extract_categories(soup)
    if categories:
        data["product_categories"] = "; ".join(categories)

    return data


def _extract_address(soup: BeautifulSoup) -> dict:
    """Try to extract structured address from the detail page."""
    result: dict[str, str] = {}

    # Find the contact heading
    contact_heading = None
    for tag in soup.find_all(["h3", "h4"]):
        if re.search(r"contact|kontakt|address|adresse", tag.get_text(), re.IGNORECASE):
            contact_heading = tag
            break

    if not contact_heading:
        return result

    # Collect text from siblings after the contact heading until the next heading
    lines = []
    for sibling in contact_heading.find_next_siblings():
        if sibling.name in ("h1", "h2", "h3", "h4"):
            break
        text = sibling.get_text("\n", strip=True)
        if text:
            lines.extend(text.split("\n"))

    # Parse address lines — typically: street, postal+city, country
    # Filter out phone/web/fax/email lines
    addr_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.match(r"(Phone|Tel|Fax|Web|E-?mail|http)", line, re.IGNORECASE):
            continue
        if re.match(r"(Phone|Tel|Fax)[:\s]", line, re.IGNORECASE):
            continue
        addr_lines.append(line)

    if len(addr_lines) >= 1:
        result["street"] = addr_lines[0]
    if len(addr_lines) >= 2:
        # Try to split "12345 City, COUNTRY" or "12345 City"
        line2 = addr_lines[1]
        # Check for comma-separated country
        if "," in line2:
            city_part, country = line2.rsplit(",", 1)
            result["country"] = country.strip()
            postal_match = re.match(r"(\S+)\s+(.+)", city_part.strip())
            if postal_match:
                result["postal_code"] = postal_match.group(1)
                result["city"] = postal_match.group(2).strip()
            else:
                result["city"] = city_part.strip()
        else:
            postal_match = re.match(r"(\S+)\s+(.+)", line2.strip())
            if postal_match:
                result["postal_code"] = postal_match.group(1)
                result["city"] = postal_match.group(2).strip()
            else:
                result["city"] = line2.strip()
    if len(addr_lines) >= 3 and "country" not in result:
        result["country"] = addr_lines[2]

    return result


def _extract_categories(soup: BeautifulSoup) -> list[str]:
    """Extract product categories from the detail page."""
    categories = []
    for tag in soup.find_all(["h3", "h4"]):
        if re.search(r"categor|kategor|product", tag.get_text(), re.IGNORECASE):
            for sibling in tag.find_next_siblings():
                if sibling.name in ("h1", "h2", "h3", "h4"):
                    break
                text = sibling.get_text(strip=True)
                if text:
                    categories.append(text)
            break
    return categories


def get_total_pages(html: str) -> int:
    """Detect the last page number from pagination links."""
    soup = BeautifulSoup(html, "lxml")
    pages = set()
    for a in soup.find_all("a", href=re.compile(r"av_page=\d+")):
        match = re.search(r"av_page=(\d+)", a["href"])
        if match:
            pages.add(int(match.group(1)))
    return max(pages) if pages else 1


async def scrape_all():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient() as client:
        # Step 1: Get page 1 and detect total pages
        print("Fetching page 1...")
        html = await fetch(client, BASE_URL)
        if not html:
            print("ERROR: Could not fetch the listing page.")
            return

        total_pages = get_total_pages(html)
        print(f"Found {total_pages} pages of exhibitors.")

        # Step 2: Collect all exhibitors from all listing pages
        all_exhibitors = parse_listing_page(html)
        print(f"  Page 1: {len(all_exhibitors)} exhibitors")

        for page_num in range(2, total_pages + 1):
            await asyncio.sleep(DELAY_S)
            url = f"{BASE_URL}?av_page={page_num}"
            print(f"Fetching page {page_num}/{total_pages}...")
            page_html = await fetch(client, url)
            if page_html:
                page_exhibitors = parse_listing_page(page_html)
                print(f"  Page {page_num}: {len(page_exhibitors)} exhibitors")
                all_exhibitors.extend(page_exhibitors)

        print(f"\nTotal exhibitors found: {len(all_exhibitors)}")

        # Step 3: Visit each detail page for enrichment
        print(f"\nFetching detail pages ({CONCURRENCY} concurrent)...\n")

        async def fetch_detail(idx: int, exhibitor: dict):
            async with semaphore:
                await asyncio.sleep(DELAY_S)
                detail_html = await fetch(client, exhibitor["detail_url"])
                if detail_html:
                    detail_data = parse_detail_page(detail_html)
                    # Merge detail data (don't overwrite listing data with empty)
                    for key, val in detail_data.items():
                        if val and (key not in exhibitor or not exhibitor.get(key)):
                            exhibitor[key] = val
                progress = f"[{idx + 1}/{len(all_exhibitors)}]"
                print(f"  {progress} {exhibitor['name']}: "
                      f"{'OK' if detail_html else 'FAILED'} "
                      f"— {exhibitor.get('city', '?')}, {exhibitor.get('country', '?')}")

        tasks = [fetch_detail(i, ex) for i, ex in enumerate(all_exhibitors)]
        await asyncio.gather(*tasks)

        # Step 4: Write CSV
        print(f"\nWriting {len(all_exhibitors)} records to {OUTPUT_FILE}...")
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_exhibitors)

        print(f"Done! Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    start = time.perf_counter()
    asyncio.run(scrape_all())
    elapsed = time.perf_counter() - start
    print(f"\nCompleted in {elapsed:.1f}s")
