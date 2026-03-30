"""
ISPO 2026 Exhibitor Scraper
Scrapes all exhibitors from ISPO 2026 via Algolia + control.buzz APIs.
No browser needed - pure HTTP requests.

APIs discovered:
1. Algolia: returns all 435 exhibitors in one call (listing data)
2. control.buzz: returns full detail per exhibitor (contacts, socials, etc.)
"""

import csv
import json
import os
import time
from pathlib import Path

import httpx

# ── Config ──────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("output/ispo")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR = OUTPUT_DIR / "raw"
RAW_DIR.mkdir(exist_ok=True)
PROGRESS_FILE = OUTPUT_DIR / "_progress.json"

ALGOLIA_APP_ID = "9WUDIPIUPO"
ALGOLIA_API_KEY = "df8a06b8eb5d9bd0839e88fc7606560f"
ALGOLIA_URL = (
    f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
    f"?x-algolia-application-id={ALGOLIA_APP_ID}"
    f"&x-algolia-api-key={ALGOLIA_API_KEY}"
)
INDEX_NAME = "production-231-9c23b24c-db23-11f0-8cd7-000000000000-exhibitors-2026-ispo"

DETAIL_BASE = (
    "https://raccoonmediagroup.control.buzz/campaign/"
    "international-running-expo-2026/web-module/exhibitors-2026-ispo/exhibitors"
)

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
}


# ── Progress tracking ───────────────────────────────────────────────────────
def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_progress(done):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f)


# ── Step 1: Fetch all exhibitors from Algolia ──────────────────────────────
def fetch_all_exhibitors(client):
    """Fetch the full exhibitor list from Algolia (all in one call)."""
    body = {
        "requests": [{
            "indexName": INDEX_NAME,
            "hitsPerPage": 1000,
            "page": 0,
            "query": "",
            "facets": ["*"],
        }]
    }
    r = client.post(
        ALGOLIA_URL,
        headers={"accept": "application/json", "content-type": "text/plain"},
        content=json.dumps(body),
    )
    r.raise_for_status()
    data = r.json()
    hits = data["results"][0]["hits"]
    total = data["results"][0]["nbHits"]
    print(f"Algolia returned {len(hits)}/{total} exhibitors")
    return hits


# ── Step 2: Fetch exhibitor detail ─────────────────────────────────────────
def fetch_detail(client, exhibitor_id):
    """Fetch full exhibitor detail from control.buzz API."""
    url = f"{DETAIL_BASE}/{exhibitor_id}"
    r = client.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json()


# ── Step 3: Flatten to CSV row ─────────────────────────────────────────────
def flatten(listing, detail):
    """Merge listing + detail data into a flat dict for CSV."""
    row = {
        "id": detail.get("id", ""),
        "name": detail.get("name", ""),
        "identifier": detail.get("identifier", ""),
        "biography": (detail.get("biography") or "").replace("\n", " ").strip(),
        "stands": ", ".join(detail.get("stands") or []),
        "featured": detail.get("featured", False),
        "website": detail.get("website") or "",
        "website_email": detail.get("website_email") or "",
        "has_contact_email": detail.get("has_contact_email", False),
        "show_email": detail.get("show_email", ""),
        "show_addresses": detail.get("show_addresses", ""),
        "show_phones": detail.get("show_phones", ""),
        "created_at": detail.get("created_at", ""),
        "detail_page_url": f"https://www.ispo.com/exhibitors-2026#/exhibitor/{detail.get('identifier', '')}",
    }

    # Addresses
    addresses = detail.get("addresses") or []
    for i, addr in enumerate(addresses[:3]):
        prefix = f"address_{i+1}_"
        row[prefix + "street"] = addr.get("street", "")
        row[prefix + "city"] = addr.get("city", "")
        row[prefix + "zip"] = addr.get("zip", "")
        row[prefix + "state"] = addr.get("state", "")
        row[prefix + "country"] = addr.get("country", "")
        row[prefix + "country_iso"] = addr.get("country_iso", "")

    # Phones
    phones = detail.get("phones") or []
    for i, phone in enumerate(phones[:3]):
        if isinstance(phone, dict):
            row[f"phone_{i+1}"] = phone.get("number", "")
            row[f"phone_{i+1}_label"] = phone.get("label", "")
        else:
            row[f"phone_{i+1}"] = str(phone)

    # Social links
    social_links = detail.get("social_links") or []
    for link in social_links:
        if isinstance(link, dict):
            platform = (link.get("platform") or link.get("label") or "").lower()
            url = link.get("url", "")
            if platform:
                row[f"social_{platform}"] = url
            else:
                # Try to identify platform from URL
                for p in ["linkedin", "facebook", "instagram", "twitter", "youtube", "tiktok"]:
                    if p in url.lower():
                        row[f"social_{p}"] = url
                        break

    # Products (just names)
    products = detail.get("products") or []
    product_names = []
    for p in products:
        if isinstance(p, dict):
            product_names.append(p.get("name", ""))
        else:
            product_names.append(str(p))
    row["products"] = "; ".join(product_names)

    # Country from listing (Algolia)
    row["country_iso"] = listing.get("country_iso") or ""

    # Logo URL
    images = detail.get("images") or []
    for img in images:
        if isinstance(img, dict) and img.get("identifier") == "profile_logo":
            row["logo_url"] = img.get("url", "")
            break

    return row


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    done = load_progress()
    print(f"Resuming with {len(done)} already completed.\n")

    with httpx.Client(follow_redirects=True, timeout=30) as client:
        # Step 1: Get all exhibitors from Algolia
        print("Step 1: Fetching exhibitor list from Algolia...")
        exhibitors = fetch_all_exhibitors(client)

        # Save listing data
        with open(OUTPUT_DIR / "exhibitors_listing.json", "w", encoding="utf-8") as f:
            json.dump(exhibitors, f, indent=2, ensure_ascii=False)
        print(f"  Saved listing data for {len(exhibitors)} exhibitors.\n")

        # Step 2: Fetch details for each exhibitor
        print("Step 2: Fetching exhibitor details...")
        all_rows = []
        errors = []
        total = len(exhibitors)

        for i, ex in enumerate(exhibitors):
            ex_id = ex.get("objectID", "")
            name = ex.get("name", "unknown")

            if ex_id in done:
                # Load from raw file
                raw_file = RAW_DIR / f"{ex_id}.json"
                if raw_file.exists():
                    with open(raw_file, encoding="utf-8") as f:
                        detail = json.load(f)
                    all_rows.append(flatten(ex, detail))
                continue

            try:
                detail = fetch_detail(client, ex_id)

                # Save raw detail
                with open(RAW_DIR / f"{ex_id}.json", "w", encoding="utf-8") as f:
                    json.dump(detail, f, indent=2, ensure_ascii=False)

                all_rows.append(flatten(ex, detail))
                done.add(ex_id)

                if (i + 1) % 10 == 0:
                    save_progress(done)
                    print(f"  [{i+1}/{total}] {name}")

                # Rate limiting: ~5 requests/sec
                time.sleep(0.2)

            except httpx.HTTPStatusError as e:
                print(f"  [{i+1}/{total}] ERROR {e.response.status_code} for {name}: {e}")
                errors.append({"id": ex_id, "name": name, "error": str(e)})
                time.sleep(1)
            except Exception as e:
                print(f"  [{i+1}/{total}] ERROR for {name}: {e}")
                errors.append({"id": ex_id, "name": name, "error": str(e)})
                time.sleep(1)

        save_progress(done)
        print(f"\n  Completed: {len(done)}/{total}")
        if errors:
            print(f"  Errors: {len(errors)}")
            with open(OUTPUT_DIR / "errors.json", "w", encoding="utf-8") as f:
                json.dump(errors, f, indent=2)

        # Step 3: Export CSV
        print("\nStep 3: Exporting CSV...")
        if all_rows:
            # Collect all unique keys
            all_keys = []
            seen_keys = set()
            for row in all_rows:
                for k in row:
                    if k not in seen_keys:
                        all_keys.append(k)
                        seen_keys.add(k)

            csv_path = OUTPUT_DIR / "ispo_exhibitors_2026.csv"
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(all_rows)
            print(f"  Saved {len(all_rows)} exhibitors to {csv_path}")

            # Also save combined JSON
            json_path = OUTPUT_DIR / "ispo_exhibitors_2026.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(all_rows, f, indent=2, ensure_ascii=False)
            print(f"  Saved JSON to {json_path}")
        else:
            print("  No rows to export!")

        print("\nDone!")


if __name__ == "__main__":
    main()
