# Cached extraction code
# URL: https://www.beauty-duesseldorf.com/vis/v1/en/directory/a
# Structural Hash: b6be34054f86ed8f5a2905049b75408d6965f632e2c7804e5d5a1f6dc298da64
# Generated at: 2026-03-11T16:24:29.513291

from bs4 import BeautifulSoup
import re
from datetime import datetime

def extract_data(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    extracted_data = []

    def safe_text(el):
        try:
            if not el:
                return None
            return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() or None
        except Exception as e:
            print(f"safe_text error: {e}")
            return None

    def safe_attr(el, attr):
        try:
            if not el:
                return None
            val = el.get(attr)
            if isinstance(val, list):
                val = " ".join([str(v) for v in val if v])
            return str(val).strip() if val else None
        except Exception as e:
            print(f"safe_attr({attr}) error: {e}")
            return None

    def empty_record():
        return {
            "name": None,
            "type": None,
            "location_text": None,
            "hall_stand": None,
            "hall_map_link": None,
            "logo_img": None,
            "details_button_text": None,
        }

    try:
        # The provided HTML sample contains no item records. This logic is designed
        # to scale to full pages where records exist in cards/listings/tables/modals.
        record_selectors = [
            "[data-exhibitor]",
            "[data-testid*='exhibitor']",
            "[class*='exhibitor']",
            "[class*='listing'] [class*='card']",
            "[class*='result'] [class*='card']",
            "article",
            "li",
            "div",
        ]

        candidates = []
        seen = set()
        for sel in record_selectors:
            try:
                for el in soup.select(sel):
                    # Heuristic: only consider as a record if it contains likely fields
                    text = safe_text(el) or ""
                    has_nameish = bool(el.select_one("h1,h2,h3,h4,[class*='name'],[data-field*='name']"))
                    has_detailish = bool(el.select_one("a[href*='detail'],a[href*='exhibitor'],button,[class*='detail']"))
                    has_locationish = bool(re.search(r"\b(hall|stand|booth)\b", text, re.I)) or bool(el.select_one("[class*='hall'],[class*='stand'],[class*='booth'],[class*='location']"))
                    if not (has_nameish or has_detailish or has_locationish):
                        continue

                    key = id(el)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(el)
            except Exception as e:
                print(f"Selector error ({sel}): {e}")

        # De-duplicate nested candidates by preferring smaller set: remove those fully contained by another
        try:
            filtered = []
            for el in candidates:
                parent_is_candidate = False
                try:
                    for p in el.parents:
                        if p in candidates:
                            parent_is_candidate = True
                            break
                except Exception:
                    pass
                if not parent_is_candidate:
                    filtered.append(el)
            candidates = filtered
        except Exception as e:
            print(f"Candidate filtering error: {e}")

        for el in candidates:
            rec = empty_record()

            # name
            try:
                name_el = el.select_one(
                    "h1,h2,h3,h4,[class*='name'],[data-field*='name'],[data-testid*='name']"
                )
                rec["name"] = safe_text(name_el)
            except Exception as e:
                print(f"name extraction error: {e}")

            # type
            try:
                type_el = el.select_one(
                    "[class*='type'],[data-field*='type'],[data-testid*='type'],[class*='category'],[class*='segment']"
                )
                rec["type"] = safe_text(type_el)
            except Exception as e:
                print(f"type extraction error: {e}")

            # location_text
            try:
                loc_el = el.select_one(
                    "[class*='location'],[data-field*='location'],[data-testid*='location'],[class*='address']"
                )
                location_text = safe_text(loc_el)
                if not location_text:
                    # fallback: parse from surrounding text
                    blob = safe_text(el) or ""
                    m = re.search(r"(Hall\s*[^,\n•|]+(?:[,|•]\s*)?\s*(Stand|Booth)\s*[^,\n•|]+)", blob, re.I)
                    if m:
                        location_text = m.group(1).strip()
                rec["location_text"] = location_text
            except Exception as e:
                print(f"location_text extraction error: {e}")

            # hall_stand
            try:
                hall = None
                stand = None

                hall_el = el.select_one("[class*='hall'],[data-field*='hall'],[data-testid*='hall']")
                stand_el = el.select_one("[class*='stand'],[class*='booth'],[data-field*='stand'],[data-field*='booth'],[data-testid*='stand'],[data-testid*='booth']")
                hall = safe_text(hall_el)
                stand = safe_text(stand_el)

                if not (hall or stand):
                    blob = safe_text(el) or ""
                    mh = re.search(r"\bHall\s*[:#-]?\s*([A-Za-z0-9\-\./]+)", blob, re.I)
                    ms = re.search(r"\b(Stand|Booth)\s*[:#-]?\s*([A-Za-z0-9\-\./]+)", blob, re.I)
                    if mh:
                        hall = mh.group(1).strip()
                    if ms:
                        stand = ms.group(2).strip()

                if hall and stand:
                    rec["hall_stand"] = f"Hall {hall} - Stand {stand}"
                elif hall:
                    rec["hall_stand"] = f"Hall {hall}"
                elif stand:
                    rec["hall_stand"] = f"Stand {stand}"
                else:
                    rec["hall_stand"] = None
            except Exception as e:
                print(f"hall_stand extraction error: {e}")

            # hall_map_link
            try:
                map_a = el.select_one(
                    "a[href*='map'],a[href*='hall'],a[href*='floor'],a[href*='plan'],a[class*='map']"
                )
                rec["hall_map_link"] = safe_attr(map_a, "href")
            except Exception as e:
                print(f"hall_map_link extraction error: {e}")

            # logo_img
            try:
                img = el.select_one("img[src],img[data-src],img[data-lazy-src],img[srcset]")
                if img:
                    rec["logo_img"] = (
                        safe_attr(img, "src")
                        or safe_attr(img, "data-src")
                        or safe_attr(img, "data-lazy-src")
                    )
                    if not rec["logo_img"]:
                        srcset = safe_attr(img, "srcset")
                        if srcset:
                            # take first URL in srcset
                            first = srcset.split(",")[0].strip().split(" ")[0].strip()
                            rec["logo_img"] = first or None
                else:
                    rec["logo_img"] = None
            except Exception as e:
                print(f"logo_img extraction error: {e}")

            # details_button_text
            try:
                details_el = el.select_one(
                    "a[href*='detail'],a[href*='exhibitor'],a[class*='detail'],button[class*='detail'],button, a[role='button']"
                )
                rec["details_button_text"] = safe_text(details_el)
            except Exception as e:
                print(f"details_button_text extraction error: {e}")

            extracted_data.append(rec)

        return extracted_data

    except Exception as e:
        print(f"Error extracting data: {e}")
        return []