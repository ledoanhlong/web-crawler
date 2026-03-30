"""
Fast enrichment for IFA exhibitors using Selenium + DuckDuckGo.
Single search per exhibitor, minimal delay, resume support.

Usage:
    python scripts/ifa_enrich_fast.py
"""

import json
import re
import sys
import io
import time
import urllib.parse
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

OUTPUT_DIR = Path("output")
ENRICH_PROGRESS_FILE = OUTPUT_DIR / "ifa_enrich_progress.json"
LISTING_PROGRESS_FILE = OUTPUT_DIR / "ifa_listing_progress.json"
DETAIL_PROGRESS_FILE = OUTPUT_DIR / "ifa_detail_progress.json"

DELAY = 1.5  # delay between searches


def load_progress(path):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_progress(path, data):
    # Safety: only save if we have more data than what's already on disk
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            old_count = len(old.get("done_slugs", []))
            new_count = len(data.get("done_slugs", []))
            if new_count < old_count:
                print(f"  WARNING: Refusing to save {new_count} items over {old_count} items!")
                return
        except Exception:
            pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def create_driver():
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    driver.set_page_load_timeout(30)
    return driver


def restart_driver(driver):
    """Safely quit and create a new driver."""
    try:
        driver.quit()
    except Exception:
        pass
    time.sleep(3)
    return create_driver()


def search_ddg(driver, query):
    """Search DuckDuckGo and return results via JS extraction."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://duckduckgo.com/?q={encoded}"
    try:
        driver.get(url)
    except Exception as e:
        # Re-raise so caller can handle with restart_driver
        raise
    time.sleep(2.5)

    results = driver.execute_script("""
        const results = [];
        // Try multiple selectors for DDG results
        const selectors = [
            'article[data-testid="result"]',
            '[data-result="web"]',
            '.react-results--main li',
            '#links .result',
        ];

        for (const sel of selectors) {
            const els = document.querySelectorAll(sel);
            if (els.length > 0) {
                els.forEach(el => {
                    const a = el.querySelector('a[href]');
                    const snippet = el.querySelector('[data-result="snippet"], .result__snippet, span');
                    if (a && a.href) {
                        results.push({
                            url: a.href,
                            title: a.innerText.trim().substring(0, 200),
                            snippet: snippet ? snippet.innerText.trim().substring(0, 400) : el.innerText.trim().substring(0, 400)
                        });
                    }
                });
                break;
            }
        }

        // Fallback: get all links that look like results
        if (results.length === 0) {
            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.href;
                if (href && !href.includes('duckduckgo.com') &&
                    !href.includes('google.com') && !href.includes('bing.com') &&
                    href.startsWith('http') && a.innerText.trim().length > 10) {
                    results.push({
                        url: href,
                        title: a.innerText.trim().substring(0, 200),
                        snippet: ''
                    });
                }
            });
        }

        return JSON.stringify(results.slice(0, 10));
    """)

    return json.loads(results) if results else []


def enrich_exhibitor(driver, name, website, country, show_area):
    """Enrich a single exhibitor with a single DuckDuckGo search."""
    enriched = {
        "linkedin_url": "",
        "parent_group": "",
        "vertical": "",
        "sub_vertical": "",
        "org_size": "",
        "competitor_info": "",
    }

    # Single combined search
    query = f'"{name}" company linkedin employees'
    if website:
        domain = website.replace("https://", "").replace("http://", "").split("/")[0]
        query = f'"{name}" OR "{domain}" linkedin company employees'

    try:
        results = search_ddg(driver, query)
    except Exception:
        results = []

    for r in results:
        url = r.get("url", "")
        snippet = r.get("snippet", "").lower()
        title = r.get("title", "").lower()

        # LinkedIn URL
        if "linkedin.com/company" in url and not enriched["linkedin_url"]:
            enriched["linkedin_url"] = url

        # Parent group hints
        for keyword in ["subsidiary of", "part of", "owned by", "a division of",
                        "acquired by", "belongs to", "parent company"]:
            if keyword in snippet and not enriched["parent_group"]:
                enriched["parent_group"] = r.get("snippet", "")[:200]
                break

        # Org size hints
        if not enriched["org_size"]:
            for size_kw in ["employees", "workforce", "team of", "staff", "people"]:
                if size_kw in snippet:
                    match = re.search(r'(\d[\d,\.]*)\s*(?:\+\s*)?(?:employees|staff|people|workforce)',
                                      snippet)
                    if match:
                        num_str = match.group(1).replace(",", "").replace(".", "")
                        try:
                            num = int(num_str)
                            if num >= 5000:
                                enriched["org_size"] = "Enterprise"
                            elif num >= 200:
                                enriched["org_size"] = "Mid-Market"
                        except ValueError:
                            pass

        # Competitor mentions
        if not enriched["competitor_info"]:
            for comp_kw in ["competitor", "competes with", "rival", "alternative to",
                            "vs ", "compared to"]:
                if comp_kw in snippet:
                    enriched["competitor_info"] = r.get("snippet", "")[:300]
                    break

    # Vertical classification from show_area
    show_area_map = {
        "Home Appliances": ("Home Appliances", "General"),
        "Computing & Gaming": ("Computing & IT", "Computing & Gaming"),
        "Smart Home": ("Smart Home / IoT", "Smart Home"),
        "Communication & Connectivity": ("Telecommunications", "Communication & Connectivity"),
        "Audio": ("Consumer Electronics", "Audio"),
        "IFA Next": ("Consumer Electronics", "Innovation / Startup"),
        "Home & Entertainment": ("Consumer Electronics", "Home & Entertainment"),
        "Fitness & Digital Health": ("Health & Wellness Tech", "Fitness & Digital Health"),
        "IFA Global Markets": ("Distribution & Retail", "Global Markets"),
    }

    if show_area in show_area_map:
        enriched["vertical"], enriched["sub_vertical"] = show_area_map[show_area]
    else:
        # Try to infer from search results
        all_text = " ".join(r.get("snippet", "") for r in results).lower()
        vertical_map = {
            "Consumer Electronics": ["consumer electronics", "electronics manufacturer",
                                     "smartphones", "mobile", "wearables", "audio", "tv"],
            "Home Appliances": ["home appliances", "kitchen appliance", "washing machine",
                                "refrigerator", "vacuum", "dishwasher", "cooking"],
            "Computing & IT": ["computing", "laptop", "desktop", "software", "semiconductor",
                               "computer", "gaming", "pc "],
            "Smart Home / IoT": ["smart home", "iot", "connected home", "home automation"],
            "Telecommunications": ["telecom", "5g", "network", "connectivity", "router", "wifi"],
            "Health & Wellness Tech": ["health tech", "fitness", "medical device", "wellness"],
            "Energy & Sustainability": ["solar", "renewable", "energy", "battery", "ev charging"],
            "Accessories & Components": ["accessories", "cable", "charger", "adapter", "case"],
            "Distribution & Retail": ["distributor", "distribution", "wholesale", "retail"],
        }
        for vertical, keywords in vertical_map.items():
            if any(kw in all_text for kw in keywords):
                enriched["vertical"] = vertical
                for kw in keywords:
                    if kw in all_text:
                        enriched["sub_vertical"] = kw.strip().title()
                        break
                break

    return enriched


def main():
    listing_data = load_progress(LISTING_PROGRESS_FILE)
    exhibitors = listing_data.get("exhibitors", [])
    detail_data = load_progress(DETAIL_PROGRESS_FILE)
    details = detail_data.get("details", {})

    progress = load_progress(ENRICH_PROGRESS_FILE)
    done_slugs = set(progress.get("done_slugs", []))
    enrichments = progress.get("enrichments", {})

    print(f"Total exhibitors: {len(exhibitors)}")
    print(f"Already enriched: {len(done_slugs)}")

    remaining = [(i, ex) for i, ex in enumerate(exhibitors) if ex["slug"] not in done_slugs]
    print(f"Remaining: {len(remaining)}")

    if not remaining:
        print("All done!")
        return

    driver = create_driver()

    try:
        # Dismiss DDG cookie banner on first visit
        try:
            driver.get("https://duckduckgo.com/")
            time.sleep(2)
            try:
                btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Accept')]"))
                )
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(1)
            except Exception:
                pass
        except Exception:
            print("Initial DDG load failed, restarting driver...")
            driver = restart_driver(driver)

        show_area_map = {
            "Home Appliances": ("Home Appliances", "General"),
            "Computing & Gaming": ("Computing & IT", "Computing & Gaming"),
            "Smart Home": ("Smart Home / IoT", "Smart Home"),
            "Communication & Connectivity": ("Telecommunications", "Communication & Connectivity"),
            "Audio": ("Consumer Electronics", "Audio"),
            "IFA Next": ("Consumer Electronics", "Innovation / Startup"),
            "Home & Entertainment": ("Consumer Electronics", "Home & Entertainment"),
            "Fitness & Digital Health": ("Health & Wellness Tech", "Fitness & Digital Health"),
            "IFA Global Markets": ("Distribution & Retail", "Global Markets"),
        }

        batch_count = 0
        consecutive_errors = 0
        for idx, (i, ex) in enumerate(remaining):
            slug = ex["slug"]
            detail = details.get(slug, {})
            if detail.get("error") == "404":
                done_slugs.add(slug)
                enrichments[slug] = {}
                continue

            name = ex["name"]
            website = detail.get("website", "")
            country = ex.get("country", "")
            show_area = ex.get("show_area", "")

            # Filter social URLs from website
            social_domains = ["tiktok.com", "instagram.com", "youtube.com", "facebook.com",
                              "twitter.com", "x.com", "linkedin.com"]
            if website and any(sd in website.lower() for sd in social_domains):
                website = ""

            print(f"  [{i+1}/{len(exhibitors)}] {name[:50]}...", end=" ", flush=True)

            try:
                enriched = enrich_exhibitor(driver, name, website, country, show_area)
                enrichments[slug] = enriched
                done_slugs.add(slug)

                consecutive_errors = 0
                linkedin = "Y" if enriched.get("linkedin_url") else "N"
                org = enriched.get("org_size", "-") or "-"
                vert = enriched.get("vertical", "-") or "-"
                print(f"LI={linkedin} size={org} vert={vert}")

                batch_count += 1

                # Save every 20
                if batch_count % 20 == 0:
                    save_progress(ENRICH_PROGRESS_FILE, {
                        "done_slugs": sorted(done_slugs),
                        "enrichments": enrichments
                    })

                time.sleep(DELAY)

            except Exception as e:
                err_msg = str(e)[:80]
                print(f"ERROR: {err_msg}")
                save_progress(ENRICH_PROGRESS_FILE, {
                    "done_slugs": sorted(done_slugs),
                    "enrichments": enrichments
                })

                # If DDG is blocking, apply show_area fallback and skip
                if "timeout" in err_msg.lower() or "renderer" in err_msg.lower():
                    print("    -> DDG blocked/timeout, restarting browser...")
                    driver = restart_driver(driver)
                    # Apply show_area fallback for this exhibitor
                    enriched_fallback = {
                        "linkedin_url": "", "parent_group": "",
                        "vertical": "", "sub_vertical": "",
                        "org_size": "", "competitor_info": "",
                    }
                    if show_area in show_area_map:
                        enriched_fallback["vertical"], enriched_fallback["sub_vertical"] = show_area_map[show_area]
                    enrichments[slug] = enriched_fallback
                    done_slugs.add(slug)
                    consecutive_errors += 1
                    # If 3+ consecutive errors, wait longer
                    if consecutive_errors >= 3:
                        print(f"    -> {consecutive_errors} consecutive errors, waiting 30s...")
                        time.sleep(30)
                    if consecutive_errors >= 10:
                        print("    -> Too many consecutive errors, stopping.")
                        break
                    time.sleep(5)
                else:
                    # Other error, try to recover
                    try:
                        driver.get("https://duckduckgo.com/")
                        time.sleep(2)
                    except Exception:
                        driver = restart_driver(driver)
                continue

    finally:
        save_progress(ENRICH_PROGRESS_FILE, {
            "done_slugs": sorted(done_slugs),
            "enrichments": enrichments
        })

        try:
            driver.quit()
        except Exception:
            pass

    # Stats
    with_linkedin = sum(1 for e in enrichments.values() if e.get("linkedin_url"))
    with_vertical = sum(1 for e in enrichments.values() if e.get("vertical"))
    with_size = sum(1 for e in enrichments.values() if e.get("org_size"))
    with_parent = sum(1 for e in enrichments.values() if e.get("parent_group"))

    print(f"\nEnrichment complete!")
    print(f"  Total: {len(enrichments)}")
    print(f"  With LinkedIn: {with_linkedin}")
    print(f"  With vertical: {with_vertical}")
    print(f"  With org size: {with_size}")
    print(f"  With parent group: {with_parent}")


if __name__ == "__main__":
    main()
