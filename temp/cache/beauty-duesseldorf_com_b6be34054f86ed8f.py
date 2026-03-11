# Cached extraction code
# URL: https://www.beauty-duesseldorf.com/vis/v1/en/directory/a
# Structural Hash: b6be34054f86ed8f5a2905049b75408d6965f632e2c7804e5d5a1f6dc298da64
# Generated at: 2026-03-11T20:08:55.030247

from bs4 import BeautifulSoup
import re
from datetime import datetime

def extract_data(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    extracted_data = []

    def safe_text(el):
        try:
            if not el:
                return ""
            return " ".join(el.get_text(" ", strip=True).split())
        except Exception:
            return ""

    def safe_attr(el, attr):
        try:
            if not el:
                return ""
            return (el.get(attr) or "").strip()
        except Exception:
            return ""

    def normalize_booth(text):
        try:
            if not text:
                return ""
            t = " ".join(text.split())
            # common patterns: "Booth: 123", "Stand 4-123", "Booth 12A"
            m = re.search(r'\b(?:booth|stand)\b\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9\-\/\s]*)', t, re.I)
            return m.group(1).strip() if m else ""
        except Exception:
            return ""

    def split_location(location_text):
        try:
            if not location_text:
                return ("", "")
            # heuristics: "City, Country" or "City - Country"
            parts = [p.strip() for p in re.split(r"\s*(?:,|\s-\s|\s\|\s)\s*", location_text) if p.strip()]
            if len(parts) >= 2:
                return (parts[0], parts[-1])
            return ("", parts[0] if parts else "")
        except Exception:
            return ("", "")

    def default_record():
        return {
            "name": "",
            "type": "",
            "location_text": "",
            "city": "",
            "country": "",
            "booth": "",
            "booth_map_link": "",
            "logo_img": "",
            "details_button": ""
        }

    try:
        # The provided HTML is a minimal shell; real content likely rendered in #finder-app.
        # Try to locate any repeated "card/list item" patterns that may appear in full HTML.
        candidate_selectors = [
            "[data-testid*='exhibitor']",
            "[class*='exhibitor']",
            "[class*='company']",
            "[class*='profile']",
            "[class*='card']",
            "article",
            "li",
            "div"
        ]

        items = []
        seen = set()

        for sel in candidate_selectors:
            try:
                for el in soup.select(sel):
                    # filter to likely records: has a name-like element or a details link/button
                    has_name = bool(el.select_one("h1,h2,h3,h4,[class*='name'],[class*='title'],a[href*='detail'],a[href*='profile']"))
                    has_logo = bool(el.select_one("img[src],[style*='background-image']"))
                    has_details = bool(el.select_one("a[href],button"))
                    if not (has_name or has_logo or has_details):
                        continue

                    key = (el.name, safe_attr(el, "id"), " ".join(el.get("class", []))[:200], str(el)[:200])
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(el)
            except Exception as e:
                print(f"Selector scan error ({sel}): {e}")

        # If nothing found, return empty list consistently
        if not items:
            return extracted_data

        for el in items:
            record = default_record()

            try:
                # name
                try:
                    name_el = el.select_one(
                        "h1,h2,h3,h4,"
                        "[class*='name'],[class*='Name'],"
                        "[class*='title'],[class*='Title'],"
                        "a[href*='detail'],a[href*='profile']"
                    )
                    record["name"] = safe_text(name_el)
                except Exception as e:
                    print(f"Name extraction error: {e}")

                # type (category/industry)
                try:
                    type_el = el.select_one(
                        "[class*='type'],[class*='Type'],"
                        "[class*='category'],[class*='Category'],"
                        "[class*='industry'],[class*='Industry'],"
                        "[data-testid*='type'],[data-testid*='category']"
                    )
                    record["type"] = safe_text(type_el)
                except Exception as e:
                    print(f"Type extraction error: {e}")

                # location_text
                try:
                    loc_el = el.select_one(
                        "[class*='location'],[class*='Location'],"
                        "[class*='address'],[class*='Address'],"
                        "[data-testid*='location'],[data-testid*='address']"
                    )
                    record["location_text"] = safe_text(loc_el)
                except Exception as e:
                    print(f"Location extraction error: {e}")

                # city/country from location_text
                try:
                    city, country = split_location(record["location_text"])
                    record["city"] = city
                    record["country"] = country
                except Exception as e:
                    print(f"City/Country parsing error: {e}")

                # booth + booth_map_link
                try:
                    booth_el = None
                    for cand in el.select(
                        "[class*='booth'],[class*='Booth'],"
                        "[class*='stand'],[class*='Stand'],"
                        "[data-testid*='booth'],[data-testid*='stand'],"
                        "a[href*='booth'],a[href*='stand'],a[href*='map'],a[href*='floor']"
                    ):
                        txt = safe_text(cand)
                        href = safe_attr(cand, "href")
                        if re.search(r"\b(?:booth|stand)\b", txt, re.I) or href:
                            booth_el = cand
                            break

                    booth_text = safe_text(booth_el)
                    record["booth"] = normalize_booth(booth_text) or booth_text

                    # map link: prefer explicit map/floor href nearby
                    map_link = ""
                    try:
                        map_a = el.select_one("a[href*='map'],a[href*='floor'],a[href*='booth'],a[href*='stand']")
                        map_link = safe_attr(map_a, "href")
                    except Exception as e:
                        print(f"Booth map link extraction error: {e}")
                    record["booth_map_link"] = map_link
                except Exception as e:
                    print(f"Booth extraction error: {e}")

                # logo_img
                try:
                    img = el.select_one("img[src],img[data-src],img[srcset]")
                    if img:
                        record["logo_img"] = safe_attr(img, "src") or safe_attr(img, "data-src") or safe_attr(img, "srcset")
                    else:
                        # background-image fallback
                        try:
                            bg_el = el.select_one("[style*='background-image']")
                            style = safe_attr(bg_el, "style")
                            m = re.search(r'background-image\s*:\s*url\((["\']?)(.*?)\1\)', style, re.I)
                            record["logo_img"] = m.group(2).strip() if m else ""
                        except Exception as e:
                            print(f"Logo background-image extraction error: {e}")
                except Exception as e:
                    print(f"Logo extraction error: {e}")

                # details_button (link or button to details)
                try:
                    details = ""
                    a = el.select_one("a[href*='detail'],a[href*='profile'],a[href],button[onclick],button")
                    if a:
                        href = safe_attr(a, "href")
                        details = href if href else safe_text(a)
                    record["details_button"] = details
                except Exception as e:
                    print(f"Details button extraction error: {e}")

            except Exception as e:
                print(f"Record extraction error: {e}")

            extracted_data.append(record)

        return extracted_data

    except Exception as e:
        print(f"Error extracting data: {e}")
        return []