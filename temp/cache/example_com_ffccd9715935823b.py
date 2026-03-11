# Cached extraction code
# URL: https://example.com/acme
# Structural Hash: ffccd9715935823b72c0d27bd3b5cea2fed05cb65561d1d469b172bbfe71021c
# Generated at: 2026-03-11T16:06:14.255922

from bs4 import BeautifulSoup
import re
from datetime import datetime

def extract_data(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    extracted_data = []

    def empty_record():
        return {
            "name": None,
            "country": None,
            "city": None,
            "address": None,
            "postal_code": None,
            "email": None,
            "phone": None,
            "website": None,
            "description": None,
            "product_categories": [],
            "brands": [],
            "logo_url": None,
            "store_url": None,
            "social_media": [],
            "industry": None,
        }

    def safe_text(el):
        try:
            return el.get_text(" ", strip=True) if el else None
        except Exception:
            return None

    def normalize_url(url):
        try:
            if not url:
                return None
            url = url.strip()
            if url.startswith("//"):
                return "https:" + url
            return url
        except Exception:
            return None

    def collect_emails(text):
        try:
            if not text:
                return []
            return list(dict.fromkeys(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, flags=re.I)))
        except Exception:
            return []

    def collect_phones(text):
        try:
            if not text:
                return []
            # Broad phone pattern; avoids being too strict across locales
            candidates = re.findall(r"(?:\+\d{1,3}[\s\-\.]?)?(?:\(?\d{2,4}\)?[\s\-\.]?)?\d{2,4}[\s\-\.]?\d{2,4}(?:[\s\-\.]?\d{2,4})", text)
            cleaned = []
            for c in candidates:
                c2 = re.sub(r"\s+", " ", c).strip(" -.")
                if len(re.sub(r"\D", "", c2)) >= 7:
                    cleaned.append(c2)
            return list(dict.fromkeys(cleaned))
        except Exception:
            return []

    def is_social(url):
        try:
            if not url:
                return False
            u = url.lower()
            return any(d in u for d in [
                "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
                "youtube.com", "tiktok.com", "pinterest.com", "snapchat.com", "threads.net",
                "wechat", "weibo.com", "vk.com"
            ])
        except Exception:
            return False

    try:
        record = empty_record()

        # Name (page-level)
        try:
            name_el = soup.select_one("h1") or soup.select_one("title")
            record["name"] = safe_text(name_el)
        except Exception as e:
            print(f"Error extracting name: {e}")

        # Description (best-effort: first meaningful paragraph)
        try:
            paras = soup.select("p")
            desc = None
            for p in paras:
                t = safe_text(p)
                if t and len(t) > 20:
                    desc = t
                    break
            record["description"] = desc
        except Exception as e:
            print(f"Error extracting description: {e}")

        # Links: website/store_url/social
        try:
            links = soup.select("a[href]")
            urls = [normalize_url(a.get("href")) for a in links]
            urls = [u for u in urls if u and not u.startswith("#") and not u.lower().startswith("javascript:")]

            social = []
            website = None
            store_url = None

            for u in urls:
                if is_social(u):
                    social.append(u)
                else:
                    if not website:
                        website = u
                    if not store_url:
                        store_url = u

            record["website"] = website
            record["store_url"] = store_url
            record["social_media"] = list(dict.fromkeys(social))
        except Exception as e:
            print(f"Error extracting links/social: {e}")

        # Email/Phone (scan full visible text + mailto/tel)
        try:
            full_text = safe_text(soup.body) or safe_text(soup) or ""
            emails = set(collect_emails(full_text))
            phones = set(collect_phones(full_text))

            for a in soup.select("a[href^='mailto:']"):
                try:
                    href = a.get("href", "")
                    em = href.split(":", 1)[1].split("?", 1)[0].strip()
                    if em:
                        emails.add(em)
                except Exception:
                    pass

            for a in soup.select("a[href^='tel:']"):
                try:
                    href = a.get("href", "")
                    ph = href.split(":", 1)[1].strip()
                    ph = re.sub(r"\s+", " ", ph)
                    if ph:
                        phones.add(ph)
                except Exception:
                    pass

            record["email"] = next(iter(emails), None)
            record["phone"] = next(iter(phones), None)
        except Exception as e:
            print(f"Error extracting email/phone: {e}")

        # Logo URL (best-effort)
        try:
            logo = None
            img = soup.select_one("img[alt*='logo' i], img[class*='logo' i], img[id*='logo' i]")
            if img and img.get("src"):
                logo = normalize_url(img.get("src"))
            if not logo:
                meta = soup.select_one("meta[property='og:image'], meta[name='og:image']")
                if meta and meta.get("content"):
                    logo = normalize_url(meta.get("content"))
            record["logo_url"] = logo
        except Exception as e:
            print(f"Error extracting logo_url: {e}")

        # Remaining fields not present in sample HTML: keep defaults
        extracted_data.append(record)
        return extracted_data

    except Exception as e:
        print(f"Error extracting data: {e}")
        return []