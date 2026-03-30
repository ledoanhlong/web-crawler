"""
IFA Berlin 2025 Exhibitor Scraper
==================================
Scrapes all exhibitors from https://www.ifa-berlin.com/exhibitors
including detail pages, then enriches via DuckDuckGo.

Usage:
    python scripts/scrape_ifa.py                  # Full run
    python scripts/scrape_ifa.py --phase listing  # Only listing pages
    python scripts/scrape_ifa.py --phase details  # Only detail pages
    python scripts/scrape_ifa.py --phase enrich   # Only enrichment
    python scripts/scrape_ifa.py --phase export   # Only export

Outputs:
    output/ifa_exhibitors_raw.json   — Raw scraped data
    output/ifa_exhibitors.csv        — Final enriched CSV
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = "https://www.ifa-berlin.com"
LISTING_URL = f"{BASE_URL}/exhibitors"
TOTAL_PAGES = 90
PER_PAGE = 20  # ~1800 exhibitors

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
SCREENSHOTS_DIR = Path("screenshots/ifa")
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

LISTING_PROGRESS_FILE = OUTPUT_DIR / "ifa_listing_progress.json"
DETAIL_PROGRESS_FILE = OUTPUT_DIR / "ifa_detail_progress.json"
ENRICH_PROGRESS_FILE = OUTPUT_DIR / "ifa_enrich_progress.json"
RAW_FILE = OUTPUT_DIR / "ifa_exhibitors_raw.json"
CSV_FILE = OUTPUT_DIR / "ifa_exhibitors.csv"

DELAY_BETWEEN_PAGES = 1.5  # seconds between listing pages
DELAY_BETWEEN_DETAILS = 1.0  # seconds between detail pages
DELAY_BETWEEN_SEARCHES = 2.0  # seconds between DuckDuckGo searches


# ---------------------------------------------------------------------------
# Driver setup
# ---------------------------------------------------------------------------
def create_driver(headless=False):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
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


def dismiss_cookies(driver):
    try:
        btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Accept')]"))
        )
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(1)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------
def load_progress(path):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_progress(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Phase 1: Listing pages
# ---------------------------------------------------------------------------
def scrape_listing_page(driver, page_num):
    """Scrape a single listing page, return list of exhibitor dicts."""
    url = f"{LISTING_URL}?page={page_num}"
    driver.get(url)
    time.sleep(2)

    # Wait for cards to load
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".brand-card"))
        )
    except Exception:
        print(f"  WARNING: No brand-card found on page {page_num}")
        return []

    # Extract card data via JS
    data = driver.execute_script("""
        const cards = document.querySelectorAll('.brand-card');
        const results = [];
        cards.forEach(card => {
            const link = card.querySelector('a.list-item-link');
            if (!link) return;

            const nameEl = card.querySelector('.name');
            const chipEl = card.querySelector('.chip.show-area');
            const hallEl = card.querySelector('.brand-location-hall');
            const standEl = card.querySelector('.brand-location-stand');
            const countryEl = card.querySelector('.country');

            // Get all location entries (some exhibitors have multiple halls)
            const locations = [];
            card.querySelectorAll('.brand-location-wrapper').forEach(loc => {
                const area = loc.querySelector('.chip.show-area');
                const halls = loc.querySelectorAll('.brand-location-hall');
                const stands = loc.querySelectorAll('.brand-location-stand');
                const hallArr = Array.from(halls).map(h => h.innerText.trim());
                const standArr = Array.from(stands).map(s => s.innerText.trim());
                locations.push({
                    show_area: area ? area.innerText.trim() : '',
                    halls: hallArr,
                    stands: standArr
                });
            });

            results.push({
                name: nameEl ? nameEl.innerText.trim() : '',
                detail_url: link.href,
                slug: link.href.split('/exhibitors/')[1] || '',
                show_area: chipEl ? chipEl.innerText.trim() : '',
                hall: hallEl ? hallEl.innerText.trim() : '',
                stand: standEl ? standEl.innerText.trim() : '',
                country: countryEl ? countryEl.innerText.trim() : '',
                locations: locations
            });
        });
        return JSON.stringify(results);
    """)

    return json.loads(data) if data else []


def run_listing_phase(driver):
    """Scrape all listing pages."""
    progress = load_progress(LISTING_PROGRESS_FILE)
    all_exhibitors = progress.get("exhibitors", [])
    done_pages = set(progress.get("done_pages", []))

    print(f"\n{'='*60}")
    print(f"PHASE 1: Scraping listing pages (1-{TOTAL_PAGES})")
    print(f"Already done: {len(done_pages)} pages, {len(all_exhibitors)} exhibitors")
    print(f"{'='*60}")

    # Navigate and dismiss cookies
    driver.get(LISTING_URL)
    time.sleep(3)
    dismiss_cookies(driver)

    for page_num in range(1, TOTAL_PAGES + 1):
        if page_num in done_pages:
            continue

        print(f"  Page {page_num}/{TOTAL_PAGES}...", end=" ", flush=True)
        try:
            exhibitors = scrape_listing_page(driver, page_num)
            print(f"found {len(exhibitors)} exhibitors")

            all_exhibitors.extend(exhibitors)
            done_pages.add(page_num)

            # Save progress every 5 pages
            if page_num % 5 == 0 or page_num == TOTAL_PAGES:
                save_progress(LISTING_PROGRESS_FILE, {
                    "done_pages": sorted(done_pages),
                    "exhibitors": all_exhibitors
                })

            time.sleep(DELAY_BETWEEN_PAGES)

        except Exception as e:
            print(f"ERROR: {e}")
            # Save what we have
            save_progress(LISTING_PROGRESS_FILE, {
                "done_pages": sorted(done_pages),
                "exhibitors": all_exhibitors
            })
            continue

    # Final save
    save_progress(LISTING_PROGRESS_FILE, {
        "done_pages": sorted(done_pages),
        "exhibitors": all_exhibitors
    })

    # Deduplicate by slug
    seen = set()
    unique = []
    for ex in all_exhibitors:
        if ex["slug"] not in seen:
            seen.add(ex["slug"])
            unique.append(ex)
    all_exhibitors = unique

    print(f"\nTotal unique exhibitors: {len(all_exhibitors)}")
    save_progress(LISTING_PROGRESS_FILE, {
        "done_pages": sorted(done_pages),
        "exhibitors": all_exhibitors
    })

    return all_exhibitors


# ---------------------------------------------------------------------------
# Phase 2: Detail pages
# ---------------------------------------------------------------------------
def scrape_detail_page(driver, exhibitor):
    """Visit an exhibitor's detail page and extract all info."""
    url = exhibitor["detail_url"]
    driver.get(url)
    time.sleep(2)

    # Check for 404
    title = driver.title or ""
    if "404" in title:
        return {"error": "404"}

    # Wait for content
    try:
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".brand-detail, h1"))
        )
    except Exception:
        pass

    data = driver.execute_script("""
        const result = {};

        // Company name
        const h1 = document.querySelector('.brand-detail h1, h1');
        result.name = h1 ? h1.innerText.trim() : '';

        // Description - try multiple selectors
        const descEl = document.querySelector('.brand-detail-description, .description');
        if (descEl) {
            result.description = descEl.innerText.trim();
        } else {
            // Get text from the brand-detail section excluding header/footer
            const bd = document.querySelector('.brand-detail');
            if (bd) {
                // Get all paragraph-like text
                const paragraphs = bd.querySelectorAll('p, .text');
                result.description = Array.from(paragraphs)
                    .map(p => p.innerText.trim())
                    .filter(t => t.length > 20)
                    .join('\\n');
            }
        }
        if (!result.description) {
            // Fallback: get main text content
            const main = document.querySelector('.brand-detail-wrapper');
            if (main) {
                const texts = main.innerText;
                // Find the description (usually after the header, before events)
                const lines = texts.split('\\n').filter(l => l.trim().length > 50);
                result.description = lines.join('\\n').substring(0, 2000);
            }
        }

        // Show areas / categories
        const chips = document.querySelectorAll('.brand-detail .chip.show-area');
        result.show_areas = Array.from(chips).map(c => c.innerText.trim());

        // Locations (hall/stand)
        const locations = [];
        document.querySelectorAll('.brand-detail .brand-location-wrapper').forEach(loc => {
            const area = loc.querySelector('.chip.show-area');
            const hall = loc.querySelector('.brand-location-hall');
            const stand = loc.querySelector('.brand-location-stand');
            locations.push({
                show_area: area ? area.innerText.trim() : '',
                hall: hall ? hall.innerText.trim() : '',
                stand: stand ? stand.innerText.trim() : ''
            });
        });
        result.locations = locations;

        // Country
        const countryEl = document.querySelector('.brand-detail .country');
        result.country = countryEl ? countryEl.innerText.trim() : '';

        // Company website - look for social-link with globe icon or explicit website link
        // The social links in the brand-detail section belong to the exhibitor
        const brandDetail = document.querySelector('.brand-detail');
        if (brandDetail) {
            const allLinks = brandDetail.querySelectorAll('a[href]');
            const externalLinks = [];
            allLinks.forEach(a => {
                const href = a.href;
                if (!href) return;
                if (href.includes('ifa-berlin.com')) return;
                if (href.startsWith('javascript:')) return;
                if (href.includes('onetrust.com')) return;
                externalLinks.push({
                    href: href,
                    text: a.innerText.trim().substring(0, 100),
                    classes: a.className
                });
            });
            result.external_links = externalLinks;
        } else {
            result.external_links = [];
        }

        // Social links specifically
        const socialLinks = document.querySelectorAll('.brand-detail .social-link a, .brand-detail a.social-link');
        result.social_links = Array.from(socialLinks).map(a => a.href);

        // Try to find website specifically - social-link with globe SVG
        const socialWrappers = document.querySelectorAll('.brand-detail .social-link');
        result.website = '';
        result.instagram = '';
        result.youtube = '';
        result.linkedin_exhibitor = '';
        result.facebook_exhibitor = '';
        result.twitter_exhibitor = '';

        socialWrappers.forEach(wrapper => {
            const link = wrapper.querySelector('a');
            if (!link) return;
            const href = link.href;
            if (href.includes('instagram.com')) result.instagram = href;
            else if (href.includes('youtube.com')) result.youtube = href;
            else if (href.includes('linkedin.com')) result.linkedin_exhibitor = href;
            else if (href.includes('facebook.com')) result.facebook_exhibitor = href;
            else if (href.includes('twitter.com') || href.includes('x.com')) result.twitter_exhibitor = href;
            else if (!href.includes('ifa-berlin.com') && !href.includes('mailto:')) {
                result.website = href;
            }
        });

        // Fallback: check for website in external links
        if (!result.website) {
            for (const link of result.external_links) {
                const h = link.href;
                if (h.startsWith('http') &&
                    !h.includes('ifa-berlin.com') &&
                    !h.includes('instagram.com') &&
                    !h.includes('youtube.com') &&
                    !h.includes('linkedin.com') &&
                    !h.includes('facebook.com') &&
                    !h.includes('twitter.com') &&
                    !h.includes('x.com') &&
                    !h.includes('mailto:') &&
                    !h.includes('maps.app.goo') &&
                    !h.includes('tel:')) {
                    result.website = h;
                    break;
                }
            }
        }

        // Emails from the brand-detail section
        const emailLinks = brandDetail ?
            Array.from(brandDetail.querySelectorAll('a[href^="mailto:"]'))
                .map(a => a.href.replace('mailto:', ''))
                .filter(e => !e.includes('ifa-management')) : [];
        result.emails = emailLinks;

        // Phone numbers
        const phoneLinks = brandDetail ?
            Array.from(brandDetail.querySelectorAll('a[href^="tel:"]'))
                .map(a => a.href.replace('tel:', '')) : [];
        result.phones = phoneLinks;

        // Events / IFA Moments
        const events = [];
        document.querySelectorAll('.ifa-moment, [class*="moment"]').forEach(ev => {
            events.push(ev.innerText.trim().substring(0, 300));
        });
        result.events = events;

        // Logo image
        const logoImg = document.querySelector('.brand-detail-header img');
        result.logo_url = logoImg ? logoImg.src : '';

        // Get full page text for the brand detail section (for later AI processing)
        if (brandDetail) {
            result.full_text = brandDetail.innerText.substring(0, 3000);
        }

        return JSON.stringify(result);
    """)

    return json.loads(data) if data else {}


def run_detail_phase(driver, exhibitors):
    """Visit each exhibitor detail page."""
    progress = load_progress(DETAIL_PROGRESS_FILE)
    done_slugs = set(progress.get("done_slugs", []))
    details = progress.get("details", {})

    print(f"\n{'='*60}")
    print(f"PHASE 2: Scraping detail pages ({len(exhibitors)} exhibitors)")
    print(f"Already done: {len(done_slugs)} details")
    print(f"{'='*60}")

    # Navigate and dismiss cookies first
    driver.get(LISTING_URL)
    time.sleep(2)
    dismiss_cookies(driver)

    batch_count = 0
    for i, ex in enumerate(exhibitors):
        slug = ex["slug"]
        if slug in done_slugs:
            continue

        print(f"  [{i+1}/{len(exhibitors)}] {ex['name'][:50]}...", end=" ", flush=True)
        try:
            detail = scrape_detail_page(driver, ex)
            if detail.get("error") == "404":
                print("404 - skipped")
                done_slugs.add(slug)
                details[slug] = {"error": "404"}
            else:
                desc_len = len(detail.get("description", ""))
                website = detail.get("website", "")
                print(f"OK (desc={desc_len}ch, web={website[:40]})")
                done_slugs.add(slug)
                details[slug] = detail

            batch_count += 1

            # Save progress every 20 items
            if batch_count % 20 == 0:
                save_progress(DETAIL_PROGRESS_FILE, {
                    "done_slugs": sorted(done_slugs),
                    "details": details
                })

            time.sleep(DELAY_BETWEEN_DETAILS)

        except Exception as e:
            print(f"ERROR: {e}")
            save_progress(DETAIL_PROGRESS_FILE, {
                "done_slugs": sorted(done_slugs),
                "details": details
            })
            # Try to recover
            try:
                driver.get(LISTING_URL)
                time.sleep(2)
            except Exception:
                pass
            continue

    # Final save
    save_progress(DETAIL_PROGRESS_FILE, {
        "done_slugs": sorted(done_slugs),
        "details": details
    })

    print(f"\nCompleted details: {len(done_slugs)}/{len(exhibitors)}")
    return details


# ---------------------------------------------------------------------------
# Phase 3: DuckDuckGo enrichment
# ---------------------------------------------------------------------------
def search_duckduckgo(driver, query):
    """Search DuckDuckGo and return top results."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://duckduckgo.com/?q={encoded}"
    driver.get(url)
    time.sleep(3)

    results = driver.execute_script("""
        const results = [];
        // DuckDuckGo result items
        document.querySelectorAll('[data-result], .react-results--main .result, article[data-testid="result"]').forEach(el => {
            const linkEl = el.querySelector('a[href]');
            const titleEl = el.querySelector('h2, [data-testid="result-title-a"]');
            const snippetEl = el.querySelector('[data-result="snippet"], .result__snippet, [data-testid="result-extras-url-link"]');
            if (linkEl) {
                results.push({
                    url: linkEl.href,
                    title: titleEl ? titleEl.innerText.trim() : '',
                    snippet: snippetEl ? snippetEl.innerText.trim() : ''
                });
            }
        });
        // Fallback: any link in results area
        if (results.length === 0) {
            document.querySelectorAll('#links .result, .nrn-react-div article').forEach(el => {
                const a = el.querySelector('a');
                if (a) {
                    results.push({
                        url: a.href,
                        title: a.innerText.trim().substring(0, 200),
                        snippet: el.innerText.trim().substring(0, 300)
                    });
                }
            });
        }
        return JSON.stringify(results.slice(0, 10));
    """)

    return json.loads(results) if results else []


def enrich_exhibitor(driver, name, website, country):
    """Search DuckDuckGo for additional info about an exhibitor."""
    enriched = {
        "linkedin_url": "",
        "parent_group": "",
        "vertical": "",
        "sub_vertical": "",
        "org_size": "",
        "competitor_info": "",
    }

    # Search 1: LinkedIn + company info
    query = f'"{name}" company linkedin'
    if website:
        domain = website.replace("https://", "").replace("http://", "").split("/")[0]
        query = f'"{name}" OR "{domain}" linkedin company'

    try:
        results = search_duckduckgo(driver, query)
        for r in results:
            url = r.get("url", "")
            snippet = r.get("snippet", "").lower()
            title = r.get("title", "").lower()

            # LinkedIn URL
            if "linkedin.com/company" in url and not enriched["linkedin_url"]:
                enriched["linkedin_url"] = url

            # Parent group hints
            for keyword in ["subsidiary of", "part of", "owned by", "a division of",
                            "acquired by", "belongs to", "group", "parent company"]:
                if keyword in snippet:
                    enriched["parent_group"] = r.get("snippet", "")[:200]
                    break

            # Org size hints
            for size_kw in ["employees", "workforce", "team of", "staff"]:
                if size_kw in snippet:
                    # Try to extract number
                    match = re.search(r'(\d[\d,]*)\s*(employees|staff|people|workforce)', snippet)
                    if match:
                        num_str = match.group(1).replace(",", "")
                        try:
                            num = int(num_str)
                            if num >= 1000:
                                enriched["org_size"] = "Enterprise" if num >= 5000 else "Mid-Market"
                            elif num >= 200:
                                enriched["org_size"] = "Mid-Market"
                        except ValueError:
                            pass

        time.sleep(DELAY_BETWEEN_SEARCHES)

    except Exception as e:
        print(f"    Search error: {e}")

    # Search 2: Industry vertical and competitor info
    query2 = f'"{name}" industry vertical market segment'
    if country:
        query2 += f" {country}"

    try:
        results2 = search_duckduckgo(driver, query2)
        snippets_text = " ".join(r.get("snippet", "") for r in results2).lower()

        # Vertical classification based on snippets
        vertical_map = {
            "Consumer Electronics": ["consumer electronics", "electronics manufacturer", "gadgets",
                                     "smartphones", "mobile devices", "wearables", "tv", "audio",
                                     "headphones", "speakers"],
            "Home Appliances": ["home appliances", "kitchen appliances", "washing machine",
                                "refrigerator", "vacuum", "dishwasher", "cooking", "oven"],
            "Computing & IT": ["computing", "laptop", "desktop", "pc", "server", "software",
                               "semiconductor", "chip", "processor", "computer"],
            "Smart Home / IoT": ["smart home", "iot", "internet of things", "connected home",
                                 "home automation", "smart device"],
            "Telecommunications": ["telecom", "5g", "network", "mobile operator", "carrier",
                                   "broadband", "fiber"],
            "Gaming & Entertainment": ["gaming", "game console", "esports", "video game",
                                       "entertainment system"],
            "Health & Wellness Tech": ["health tech", "fitness", "medical device", "wellness",
                                       "digital health", "healthtech"],
            "Energy & Sustainability": ["solar", "renewable", "energy storage", "battery",
                                        "sustainability", "green tech", "ev charging"],
            "Automotive Tech": ["automotive", "connected car", "ev", "electric vehicle",
                                "autonomous driving"],
            "Retail / E-commerce": ["retail", "e-commerce", "marketplace", "online shopping"],
            "Media & Content": ["media", "streaming", "content", "broadcast", "publishing"],
        }

        for vertical, keywords in vertical_map.items():
            if any(kw in snippets_text for kw in keywords):
                enriched["vertical"] = vertical
                # Sub-vertical: pick the most specific matching keyword
                for kw in keywords:
                    if kw in snippets_text:
                        enriched["sub_vertical"] = kw.title()
                        break
                break

        # Competitor mentions
        for r in results2:
            snippet = r.get("snippet", "").lower()
            for comp_kw in ["competitor", "competes with", "rival", "alternative to",
                            "vs ", "compared to", "competing"]:
                if comp_kw in snippet:
                    enriched["competitor_info"] = r.get("snippet", "")[:300]
                    break

        time.sleep(DELAY_BETWEEN_SEARCHES)

    except Exception as e:
        print(f"    Search error: {e}")

    return enriched


def run_enrich_phase(driver, exhibitors, details):
    """Enrich exhibitors with DuckDuckGo data."""
    progress = load_progress(ENRICH_PROGRESS_FILE)
    done_slugs = set(progress.get("done_slugs", []))
    enrichments = progress.get("enrichments", {})

    print(f"\n{'='*60}")
    print(f"PHASE 3: DuckDuckGo enrichment ({len(exhibitors)} exhibitors)")
    print(f"Already done: {len(done_slugs)} enrichments")
    print(f"{'='*60}")

    batch_count = 0
    for i, ex in enumerate(exhibitors):
        slug = ex["slug"]
        if slug in done_slugs:
            continue

        detail = details.get(slug, {})
        if detail.get("error") == "404":
            done_slugs.add(slug)
            enrichments[slug] = {}
            continue

        name = ex["name"]
        website = detail.get("website", "")
        country = ex.get("country", "")

        print(f"  [{i+1}/{len(exhibitors)}] {name[:50]}...", end=" ", flush=True)

        try:
            enriched = enrich_exhibitor(driver, name, website, country)
            enrichments[slug] = enriched
            done_slugs.add(slug)

            linkedin = enriched.get("linkedin_url", "")
            org = enriched.get("org_size", "")
            vert = enriched.get("vertical", "")
            print(f"LI={'Y' if linkedin else 'N'} size={org or '-'} vert={vert or '-'}")

            batch_count += 1

            # Save every 10
            if batch_count % 10 == 0:
                save_progress(ENRICH_PROGRESS_FILE, {
                    "done_slugs": sorted(done_slugs),
                    "enrichments": enrichments
                })

        except Exception as e:
            print(f"ERROR: {e}")
            save_progress(ENRICH_PROGRESS_FILE, {
                "done_slugs": sorted(done_slugs),
                "enrichments": enrichments
            })
            continue

    save_progress(ENRICH_PROGRESS_FILE, {
        "done_slugs": sorted(done_slugs),
        "enrichments": enrichments
    })

    print(f"\nCompleted enrichments: {len(done_slugs)}/{len(exhibitors)}")
    return enrichments


# ---------------------------------------------------------------------------
# Phase 4: Export
# ---------------------------------------------------------------------------
def run_export(exhibitors, details, enrichments):
    """Merge all data and export to CSV + JSON."""
    print(f"\n{'='*60}")
    print("PHASE 4: Exporting results")
    print(f"{'='*60}")

    records = []
    for ex in exhibitors:
        slug = ex["slug"]
        detail = details.get(slug, {})
        enrich = enrichments.get(slug, {})

        if detail.get("error") == "404":
            continue

        # Merge locations
        locs = detail.get("locations", ex.get("locations", []))
        halls = []
        stands = []
        show_areas = []
        for loc in locs:
            if isinstance(loc, dict):
                if loc.get("hall"):
                    halls.append(loc["hall"])
                if loc.get("stand"):
                    stands.append(loc["stand"])
                if loc.get("show_area"):
                    show_areas.append(loc["show_area"])
                # Handle listing format with arrays
                if loc.get("halls"):
                    halls.extend(loc["halls"])
                if loc.get("stands"):
                    stands.extend(loc["stands"])

        # Determine LinkedIn: prefer exhibitor's own, then enrichment
        # Filter out IFA's own LinkedIn (company/37122179)
        linkedin_raw = detail.get("linkedin_exhibitor", "") or enrich.get("linkedin_url", "")
        enrich_linkedin = enrich.get("linkedin_url", "")
        linkedin = ""
        if linkedin_raw and "37122179" not in linkedin_raw:
            linkedin = linkedin_raw
        elif enrich_linkedin:
            linkedin = enrich_linkedin

        # Fix website: filter out social media URLs captured as websites
        website = detail.get("website", "")
        social_domains = ["tiktok.com", "instagram.com", "youtube.com", "facebook.com",
                          "twitter.com", "x.com", "linkedin.com", "pinterest.com"]
        if website and any(sd in website.lower() for sd in social_domains):
            # Move it to the right social field if not already set
            if "tiktok.com" in website.lower():
                if not record.get("tiktok"):
                    pass  # we'll add tiktok field
            website = ""

        record = {
            "company_name": detail.get("name") or ex.get("name", ""),
            "slug": slug,
            "detail_url": ex.get("detail_url", ""),
            "description": detail.get("description", ""),
            "show_areas": "; ".join(dict.fromkeys(show_areas)),  # dedupe, preserve order
            "halls": "; ".join(dict.fromkeys(halls)),
            "stands": "; ".join(dict.fromkeys(stands)),
            "country": detail.get("country") or ex.get("country", ""),
            "website": website,
            "linkedin": linkedin,
            "instagram": detail.get("instagram", ""),
            "youtube": detail.get("youtube", ""),
            "facebook": detail.get("facebook_exhibitor", ""),
            "twitter": detail.get("twitter_exhibitor", ""),
            "emails": "; ".join(detail.get("emails", [])),
            "phones": "; ".join(detail.get("phones", [])),
            "logo_url": detail.get("logo_url", ""),
            "events": " | ".join(detail.get("events", [])),
            # Enrichment fields
            "parent_group": enrich.get("parent_group", ""),
            "vertical": enrich.get("vertical", ""),
            "sub_vertical": enrich.get("sub_vertical", ""),
            "org_size": enrich.get("org_size", ""),
            "competitor_info": enrich.get("competitor_info", ""),
        }
        records.append(record)

    # Save JSON
    with open(RAW_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(records)} records to {RAW_FILE}")

    # Save CSV
    if records:
        fieldnames = list(records[0].keys())
        with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)
        print(f"Saved {len(records)} records to {CSV_FILE}")

    # Stats
    with_website = sum(1 for r in records if r["website"])
    with_linkedin = sum(1 for r in records if r["linkedin"])
    with_desc = sum(1 for r in records if r["description"])
    with_org = sum(1 for r in records if r["org_size"])
    with_vertical = sum(1 for r in records if r["vertical"])

    print(f"\n  Stats:")
    print(f"    Total exhibitors: {len(records)}")
    print(f"    With website:     {with_website}")
    print(f"    With LinkedIn:    {with_linkedin}")
    print(f"    With description: {with_desc}")
    print(f"    With org size:    {with_org}")
    print(f"    With vertical:    {with_vertical}")

    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="IFA Berlin Exhibitor Scraper")
    parser.add_argument("--phase", choices=["listing", "details", "enrich", "export", "all"],
                        default="all", help="Which phase to run")
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    args = parser.parse_args()

    driver = None

    try:
        if args.phase in ("all", "listing"):
            driver = create_driver(headless=args.headless)
            exhibitors = run_listing_phase(driver)
            if args.phase == "listing":
                driver.quit()
                return

        if args.phase in ("all", "details"):
            # Load exhibitors from listing progress
            listing_data = load_progress(LISTING_PROGRESS_FILE)
            exhibitors = listing_data.get("exhibitors", [])
            if not exhibitors:
                print("ERROR: No exhibitors found. Run listing phase first.")
                return

            if not driver:
                driver = create_driver(headless=args.headless)
            details = run_detail_phase(driver, exhibitors)
            if args.phase == "details":
                driver.quit()
                return

        if args.phase in ("all", "enrich"):
            listing_data = load_progress(LISTING_PROGRESS_FILE)
            exhibitors = listing_data.get("exhibitors", [])
            detail_data = load_progress(DETAIL_PROGRESS_FILE)
            details = detail_data.get("details", {})

            if not exhibitors:
                print("ERROR: No exhibitors found. Run listing phase first.")
                return

            if not driver:
                driver = create_driver(headless=args.headless)
            enrichments = run_enrich_phase(driver, exhibitors, details)
            if args.phase == "enrich":
                driver.quit()
                return

        if args.phase in ("all", "export"):
            listing_data = load_progress(LISTING_PROGRESS_FILE)
            exhibitors = listing_data.get("exhibitors", [])
            detail_data = load_progress(DETAIL_PROGRESS_FILE)
            details = detail_data.get("details", {})
            enrich_data = load_progress(ENRICH_PROGRESS_FILE)
            enrichments = enrich_data.get("enrichments", {})

            if not exhibitors:
                print("ERROR: No exhibitors found. Run listing phase first.")
                return

            run_export(exhibitors, details, enrichments)

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
