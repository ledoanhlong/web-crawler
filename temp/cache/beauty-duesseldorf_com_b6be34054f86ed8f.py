# Cached extraction code
# URL: https://www.beauty-duesseldorf.com/vis/v1/en/directory/a
# Structural Hash: b6be34054f86ed8f5a2905049b75408d6965f632e2c7804e5d5a1f6dc298da64
# Generated at: 2026-03-11T23:56:47.803504

from bs4 import BeautifulSoup
import re
from datetime import datetime

def extract_data(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    extracted_data = []

    def safe_get_text(el):
        try:
            return el.get_text(" ", strip=True)
        except Exception:
            return None

    def safe_get_attr(el, attr):
        try:
            val = el.get(attr)
            return val.strip() if isinstance(val, str) else val
        except Exception:
            return None

    def make_record():
        return {
            "name": None,
            "listing_type": None,
            "location_text": None,
            "hall_stand": None,
            "hall_map_url": None,
            "logo_img": None,
            "details_button": None,
        }

    try:
        # The provided HTML sample shows a JS app mount point; full data likely renders client-side.
        # Still attempt robust extraction for server-rendered/fallback content in full HTML.

        container = None
        try:
            container = soup.select_one("#finder-app") or soup.select_one("main#main") or soup.body
        except Exception as e:
            print(f"Error locating container: {e}")
            container = soup

        # Find likely "card"/"listing" elements broadly (scales to many items)
        candidates = []
        try:
            candidates = container.select(
                "article, li, div[class*='card'], div[class*='listing'], div[class*='result'], div[class*='exhibitor']"
            )
        except Exception as e:
            print(f"Error selecting candidate elements: {e}")
            candidates = []

        # If overly broad selection yields nothing, fallback to elements that look like records
        if not candidates:
            try:
                candidates = container.find_all(["article", "li", "div"], recursive=True)
            except Exception as e:
                print(f"Error in fallback candidates: {e}")
                candidates = []

        # Heuristic: keep only elements that contain at least one likely field marker
        filtered = []
        for el in candidates:
            try:
                if el.select_one("a[href*='detail'], a[href*='exhib'], a[href*='profile'], button, h1, h2, h3, img"):
                    filtered.append(el)
            except Exception:
                continue

        # Deduplicate by object id and favor smaller "card-like" nodes
        seen = set()
        items = []
        for el in filtered:
            try:
                if id(el) in seen:
                    continue
                seen.add(id(el))
                items.append(el)
            except Exception:
                continue

        for item in items:
            record = make_record()

            # name
            try:
                name_el = (
                    item.select_one("[data-testid*='name'], [data-test*='name'], [class*='name']")
                    or item.select_one("h1, h2, h3")
                    or item.select_one("a[title]")
                )
                txt = safe_get_text(name_el)
                if not txt and name_el and name_el.name == "a":
                    txt = safe_get_attr(name_el, "title")
                record["name"] = txt
            except Exception as e:
                print(f"Error extracting name: {e}")

            # listing_type
            try:
                lt_el = (
                    item.select_one("[data-testid*='type'], [data-test*='type'], [class*='type'], [class*='category']")
                    or item.find(string=re.compile(r"\b(Sponsor|Exhibitor|Partner|Vendor|Presenter|Speaker)\b", re.I))
                )
                if hasattr(lt_el, "get_text"):
                    record["listing_type"] = safe_get_text(lt_el)
                elif isinstance(lt_el, str):
                    record["listing_type"] = lt_el.strip()
            except Exception as e:
                print(f"Error extracting listing_type: {e}")

            # location_text
            try:
                loc_el = (
                    item.select_one("[data-testid*='location'], [data-test*='location'], [class*='location'], [class*='address']")
                    or item.find(string=re.compile(r"\bHall\b|\bStand\b|\bBooth\b|\bPavilion\b", re.I))
                )
                if hasattr(loc_el, "get_text"):
                    record["location_text"] = safe_get_text(loc_el)
                elif isinstance(loc_el, str):
                    record["location_text"] = loc_el.strip()
            except Exception as e:
                print(f"Error extracting location_text: {e}")

            # hall_stand (try explicit hall/stand/booth fields, else parse from location_text)
            try:
                hs_el = (
                    item.select_one("[data-testid*='stand'], [data-test*='stand'], [class*='stand'], [class*='booth']")
                    or item.find(string=re.compile(r"\b(stand|booth)\b", re.I))
                )
                hs = None
                if hasattr(hs_el, "get_text"):
                    hs = safe_get_text(hs_el)
                elif isinstance(hs_el, str):
                    hs = hs_el.strip()

                if not hs and record.get("location_text"):
                    m = re.search(r"\b(?:Hall\s*[-:]?\s*)?([A-Z0-9]+)\b.*?\b(?:Stand|Booth)\s*[-:]?\s*([A-Z0-9\-]+)\b", record["location_text"], re.I)
                    if m:
                        hs = f"Hall {m.group(1)} Stand {m.group(2)}"
                    else:
                        m2 = re.search(r"\b(?:Stand|Booth)\s*[-:]?\s*([A-Z0-9\-]+)\b", record["location_text"], re.I)
                        if m2:
                            hs = m2.group(0)

                record["hall_stand"] = hs
            except Exception as e:
                print(f"Error extracting hall_stand: {e}")

            # hall_map_url
            try:
                map_el = item.select_one("a[href*='map'], a[href*='hall'], a[href*='floorplan'], a[aria-label*='map' i]")
                record["hall_map_url"] = safe_get_attr(map_el, "href")
            except Exception as e:
                print(f"Error extracting hall_map_url: {e}")

            # logo_img
            try:
                img_el = (
                    item.select_one("img[class*='logo'], img[data-testid*='logo'], img[alt*='logo' i]")
                    or item.select_one("img")
                )
                record["logo_img"] = safe_get_attr(img_el, "src") or safe_get_attr(img_el, "data-src")
            except Exception as e:
                print(f"Error extracting logo_img: {e}")

            # details_button
            try:
                det_el = (
                    item.select_one("a[href*='detail'], a[href*='exhib'], a[href*='profile'], a[aria-label*='details' i], a[class*='detail']")
                    or item.select_one("button[aria-label*='details' i], button[class*='detail']")
                )
                if det_el:
                    if det_el.name == "a":
                        record["details_button"] = safe_get_attr(det_el, "href")
                    else:
                        # buttons may rely on JS; return label/text as fallback
                        record["details_button"] = safe_get_attr(det_el, "data-href") or safe_get_text(det_el)
            except Exception as e:
                print(f"Error extracting details_button: {e}")

            # Keep only plausible records; but always safe/consistent fields
            try:
                # Minimal plausibility: name or details link or logo
                if record["name"] or record["details_button"] or record["logo_img"]:
                    extracted_data.append(record)
            except Exception as e:
                print(f"Error appending record: {e}")

        return extracted_data

    except Exception as e:
        print(f"Error extracting data: {e}")
        return []