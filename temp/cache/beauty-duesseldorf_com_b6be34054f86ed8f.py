# Cached extraction code
# URL: https://www.beauty-duesseldorf.com/vis/v1/en/directory/a
# Structural Hash: b6be34054f86ed8f5a2905049b75408d6965f632e2c7804e5d5a1f6dc298da64
# Generated at: 2026-03-13T19:47:58.299439

from bs4 import BeautifulSoup
import re
from datetime import datetime

def extract_data(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    extracted_data = []

    def empty_record():
        return {
            "name": None,
            "description": None,
            "country": None,
            "city": None,
            "address": None,
            "postal_code": None,
            "email": None,
            "phone": None,
            "website": None,
            "store_url": None,
            "product_categories": [],
            "brands": [],
            "booth": None,
            "hall": None,
            "social_media": {},
            "logo_url": None,
            "hall_map_url": None,
            "type_label": None,
            "details_button": None,
            "detail_link": None,
        }

    def safe_text(el):
        try:
            if not el:
                return None
            txt = el.get_text(" ", strip=True)
            return txt if txt else None
        except Exception:
            return None

    def safe_attr(el, attr):
        try:
            if not el:
                return None
            val = el.get(attr)
            if isinstance(val, list):
                val = " ".join(val)
            return val if val else None
        except Exception:
            return None

    try:
        # The provided HTML is a minimal shell; data likely rendered client-side into #finder-app.
        # We still implement robust patterns to support the full HTML if server-rendered content exists.
        # Candidate selectors for exhibitor/item cards, list rows, profile pages, etc.
        item_selectors = [
            "[data-exhibitor], [data-exhibitor-id], [data-company-id]",
            ".exhibitor, .exhibitor-card, .exhibitor-item, .company-card, .company-item",
            ".finder-result, .finder-results__item, .search-result, .search-results__item",
            "article.exhibitor, article.company, article.result, article.card",
            "li.exhibitor, li.company, li.result, li.card",
            "[class*='exhibitor'], [class*='company'][class*='card'], [class*='result'][class*='item']",
        ]

        items = []
        for sel in item_selectors:
            try:
                found = soup.select(sel)
                if found:
                    items.extend(found)
            except Exception as e:
                print(f"Selector error '{sel}': {e}")

        # Deduplicate items while preserving order
        seen = set()
        deduped_items = []
        for it in items:
            try:
                key = id(it)
                if key not in seen:
                    seen.add(key)
                    deduped_items.append(it)
            except Exception:
                continue

        # If no items found, return an empty list (consistent with template)
        if not deduped_items:
            return extracted_data

        for item in deduped_items:
            record = empty_record()

            try:
                # NAME
                try:
                    name_el = (
                        item.select_one("[data-field='name'], .name, .company-name, .exhibitor-name, .card__title, h1, h2, h3")
                    )
                    record["name"] = safe_text(name_el)
                except Exception as e:
                    print(f"Name extraction error: {e}")

                # DESCRIPTION
                try:
                    desc_el = item.select_one(
                        "[data-field='description'], .description, .company-description, .exhibitor-description, .card__summary, .summary, p"
                    )
                    record["description"] = safe_text(desc_el)
                except Exception as e:
                    print(f"Description extraction error: {e}")

                # DETAIL LINK / STORE URL / WEBSITE / DETAILS BUTTON
                try:
                    details_a = item.select_one(
                        "a[href*='detail'], a[href*='exhibitor'], a[href*='company'], a.details, a.button, a.btn, a[aria-label*='Details']"
                    )
                    record["details_button"] = safe_text(details_a)
                    record["detail_link"] = safe_attr(details_a, "href")
                except Exception as e:
                    print(f"Detail link extraction error: {e}")

                try:
                    # Website often present as external link
                    web_a = item.select_one(
                        "a[href^='http']:not([href*='facebook.']):not([href*='instagram.']):not([href*='linkedin.']):not([href*='twitter.']):not([href*='x.com']):not([href*='youtube.']):not([href*='tiktok.'])"
                    )
                    record["website"] = safe_attr(web_a, "href")
                except Exception as e:
                    print(f"Website extraction error: {e}")

                try:
                    store_a = item.select_one("a[href*='shop'], a[href*='store'], a.store, a.shop")
                    record["store_url"] = safe_attr(store_a, "href")
                except Exception as e:
                    print(f"Store URL extraction error: {e}")

                # CONTACT: EMAIL / PHONE
                try:
                    email_a = item.select_one("a[href^='mailto:']")
                    email_href = safe_attr(email_a, "href")
                    if email_href:
                        record["email"] = re.sub(r"^mailto:\s*", "", email_href, flags=re.I).split("?")[0].strip() or None
                    else:
                        # fallback: text search
                        txt = safe_text(item)
                        if txt:
                            m = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", txt, flags=re.I)
                            record["email"] = m.group(1) if m else None
                except Exception as e:
                    print(f"Email extraction error: {e}")

                try:
                    phone_a = item.select_one("a[href^='tel:']")
                    phone_href = safe_attr(phone_a, "href")
                    if phone_href:
                        record["phone"] = re.sub(r"^tel:\s*", "", phone_href, flags=re.I).strip() or None
                    else:
                        txt = safe_text(item)
                        if txt:
                            m = re.search(r"(\+?\d[\d\s().-]{6,}\d)", txt)
                            record["phone"] = m.group(1).strip() if m else None
                except Exception as e:
                    print(f"Phone extraction error: {e}")

                # LOCATION: COUNTRY / CITY / ADDRESS / POSTAL CODE
                try:
                    # Common patterns: elements with labels or address blocks
                    address_block = item.select_one(
                        "address, .address, .company-address, .location, .contact__address, [data-field*='address']"
                    )
                    address_text = safe_text(address_block)
                    if address_text:
                        record["address"] = address_text
                        # Try parse postal code and city/country heuristically
                        # Postal code: common alphanum patterns
                        mpc = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}|\d{4,6}|\d{5}(?:-\d{4})?)\b", address_text, flags=re.I)
                        record["postal_code"] = mpc.group(1).strip() if mpc else record["postal_code"]
                except Exception as e:
                    print(f"Address extraction error: {e}")

                try:
                    # Explicit fields if present
                    country_el = item.select_one("[data-field='country'], .country, .contact__country")
                    city_el = item.select_one("[data-field='city'], .city, .contact__city")
                    postal_el = item.select_one("[data-field='postal'], [data-field='postal_code'], .postal, .postal-code, .zip")
                    addr_el = item.select_one("[data-field='address'], .street, .street-address")

                    record["country"] = safe_text(country_el) or record["country"]
                    record["city"] = safe_text(city_el) or record["city"]
                    record["postal_code"] = safe_text(postal_el) or record["postal_code"]
                    # Prefer explicit street address if available
                    explicit_addr = safe_text(addr_el)
                    if explicit_addr:
                        record["address"] = explicit_addr
                except Exception as e:
                    print(f"Country/City/Postal extraction error: {e}")

                # BOOTH / HALL
                try:
                    booth_el = item.select_one(
                        "[data-field='booth'], .booth, .stand, .stand-number, .booth-number, [class*='booth'], [class*='stand']"
                    )
                    hall_el = item.select_one(
                        "[data-field='hall'], .hall, .hall-number, [class*='hall']"
                    )
                    booth_text = safe_text(booth_el)
                    hall_text = safe_text(hall_el)

                    # Normalize booth/hall if embedded in generic text
                    if not booth_text:
                        txt = safe_text(item)
                        if txt:
                            m = re.search(r"\b(?:Booth|Stand)\s*[:#]?\s*([A-Z0-9\-\/ ]{1,20})\b", txt, flags=re.I)
                            booth_text = m.group(1).strip() if m else None
                    if not hall_text:
                        txt = safe_text(item)
                        if txt:
                            m = re.search(r"\bHall\s*[:#]?\s*([A-Z0-9\-\/ ]{1,20})\b", txt, flags=re.I)
                            hall_text = m.group(1).strip() if m else None

                    record["booth"] = booth_text or None
                    record["hall"] = hall_text or None
                except Exception as e:
                    print(f"Booth/Hall extraction error: {e}")

                # PRODUCT CATEGORIES / BRANDS
                try:
                    cats = []
                    for el in item.select(
                        "[data-field='category'], [data-field='categories'], .categories li, .product-categories li, .category, .tags .tag, .tag"
                    ):
                        t = safe_text(el)
                        if t:
                            cats.append(t)
                    record["product_categories"] = list(dict.fromkeys(cats)) if cats else []
                except Exception as e:
                    print(f"Product categories extraction error: {e}")

                try:
                    brands = []
                    for el in item.select(
                        "[data-field='brand'], [data-field='brands'], .brands li, .brand, .brand-list li"
                    ):
                        t = safe_text(el)
                        if t:
                            brands.append(t)
                    record["brands"] = list(dict.fromkeys(brands)) if brands else []
                except Exception as e:
                    print(f"Brands extraction error: {e}")

                # TYPE LABEL
                try:
                    type_el = item.select_one("[data-field='type'], .type, .type-label, .label, .badge")
                    record["type_label"] = safe_text(type_el)
                except Exception as e:
                    print(f"Type label extraction error: {e}")

                # LOGO URL
                try:
                    logo_el = item.select_one("img.logo, img[alt*='logo' i], .logo img, img[class*='logo']")
                    record["logo_url"] = safe_attr(logo_el, "src") or safe_attr(logo_el, "data-src")
                except Exception as e:
                    print(f"Logo URL extraction error: {e}")

                # HALL MAP URL
                try:
                    hall_map_a = item.select_one("a[href*='map'], a[href*='hall-map'], a.hall-map, a.map")
                    record["hall_map_url"] = safe_attr(hall_map_a, "href")
                except Exception as e:
                    print(f"Hall map URL extraction error: {e}")

                # SOCIAL MEDIA
                try:
                    social = {}
                    for a in item.select("a[href]"):
                        href = safe_attr(a, "href")
                        if not href:
                            continue
                        h = href.lower()
                        if "facebook.com" in h:
                            social["facebook"] = href
                        elif "instagram.com" in h:
                            social["instagram"] = href
                        elif "linkedin.com" in h:
                            social["linkedin"] = href
                        elif "twitter.com" in h or "x.com" in h:
                            social["twitter"] = href
                        elif "youtube.com" in h or "youtu.be" in h:
                            social["youtube"] = href
                        elif "tiktok.com" in h:
                            social["tiktok"] = href
                    record["social_media"] = social if social else {}
                except Exception as e:
                    print(f"Social media extraction error: {e}")

            except Exception as e:
                print(f"Record extraction error: {e}")

            extracted_data.append(record)

        return extracted_data

    except Exception as e:
        print(f"Error extracting data: {e}")
        return []