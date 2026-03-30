#!/usr/bin/env python3
"""Import all existing scraped data into the SQLite database.

Reads from:
  - Database/  (ECDB data + raw scraped data from Phase 1)
  - output/    (crawler output: IFA, IAW, ISPO, Modefabriek)

Data cleaning:
  - Drops columns that are >95% empty
  - Strips whitespace, normalizes NaN/None/empty
  - Deduplicates companies by normalized name
  - Routes data to correct tables (events, marketplaces, financials)

Usage:
    python scripts/import_to_db.py [--db-path data/crawler.db]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.services.database import (
    CrawlerDatabase,
    _clean_float,
    _clean_int,
    _clean_str,
    normalize_name,
)


# ── Cleaning helpers ────────────────────────────────────────────────────────

def clean_dataframe(df: pd.DataFrame, drop_threshold: float = 0.95) -> pd.DataFrame:
    """Clean a DataFrame: drop near-empty columns, strip strings, normalize nulls."""
    # Drop columns that are >threshold% empty
    null_ratio = df.isna().sum() / len(df)
    empty_cols = null_ratio[null_ratio >= drop_threshold].index.tolist()

    # Also count string columns with empty/whitespace values
    for col in df.columns:
        if col in empty_cols:
            continue
        if df[col].dtype == object:
            non_null = df[col].dropna()
            if len(non_null) == 0:
                empty_cols.append(col)
                continue
            empty_str_ratio = (
                non_null.astype(str).str.strip().isin(["", "nan", "None", "N/A", "null"])
            ).sum() / len(df)
            if empty_str_ratio >= drop_threshold:
                empty_cols.append(col)

    if empty_cols:
        df = df.drop(columns=empty_cols)

    # Strip string columns
    for col in df.select_dtypes(include=["object"]).columns:
        df[col] = df[col].apply(
            lambda x: None if pd.isna(x) or str(x).strip().lower() in ("nan", "none", "n/a", "null", "")
            else str(x).strip()
        )

    return df


def extract_socials(row: dict, prefix: str = "social_") -> dict[str, str]:
    """Extract social media columns from a row."""
    socials = {}
    for key, val in row.items():
        if key.startswith(prefix) and val and str(val).strip().lower() not in ("nan", "none", ""):
            platform = key[len(prefix):]
            socials[platform] = str(val).strip()
    return socials


def extract_extras(row: dict, prefix: str = "extra_") -> dict[str, str]:
    """Extract extra_* columns into a dict, skipping empties."""
    extras = {}
    for key, val in row.items():
        if key.startswith(prefix) and val and str(val).strip().lower() not in ("nan", "none", ""):
            field = key[len(prefix):]
            extras[field] = str(val).strip()
    return extras


# ── Event importers ─────────────────────────────────────────────────────────

def import_event_csv(
    db: CrawlerDatabase,
    filepath: Path,
    event_name: str,
    event_year: int | None = 2026,
    name_col: str = "name",
    hall_col: str | None = None,
    stand_col: str | None = None,
    country_col: str = "country",
    city_col: str = "city",
    website_col: str = "website",
    email_col: str = "email",
    phone_col: str = "phone",
    description_col: str = "description",
) -> int:
    """Generic event/exhibitor CSV importer."""
    print(f"  Importing {event_name} from {filepath.name}...")
    df = pd.read_csv(filepath, dtype=str)
    original_rows = len(df)
    df = clean_dataframe(df)

    # Skip if name column missing
    if name_col not in df.columns:
        print(f"    SKIP: no '{name_col}' column found")
        return 0

    # Drop rows with no name
    df = df.dropna(subset=[name_col])
    df = df[df[name_col].str.strip().str.len() > 0]

    event_id = db.get_or_create_event(event_name, year=event_year)
    imported = 0

    for _, row in df.iterrows():
        rd = row.to_dict()
        name = _clean_str(rd.get(name_col))
        if not name:
            continue

        socials = extract_socials(rd)
        extras = extract_extras(rd)

        # Detect hall/stand columns
        hall = _clean_str(rd.get(hall_col)) if hall_col and hall_col in rd else None
        stand = None
        for sc in (stand_col, "booth_or_stand", "stand", "booth", "extra_stand", "extra_stand_number"):
            if sc and sc in rd:
                stand = _clean_str(rd.get(sc))
                if stand:
                    break

        # Product categories — may be string or list-like
        cats_raw = _clean_str(rd.get("product_categories"))
        categories = [c.strip() for c in cats_raw.split(";")] if cats_raw else []

        brands_raw = _clean_str(rd.get("brands"))
        brands = [b.strip() for b in brands_raw.split(";")] if brands_raw else []

        company_id = db.get_or_create_company(
            name=name,
            description=_clean_str(rd.get(description_col)),
            country=_clean_str(rd.get(country_col)),
            city=_clean_str(rd.get(city_col)),
            address=_clean_str(rd.get("address")),
            postal_code=_clean_str(rd.get("postal_code")),
            website=_clean_str(rd.get(website_col)),
            email=_clean_str(rd.get(email_col)),
            phone=_clean_str(rd.get(phone_col)),
            fax=_clean_str(rd.get("fax")),
            logo_url=_clean_str(rd.get("logo_url")),
        )

        for platform, url in socials.items():
            db.upsert_social(company_id, platform, url)

        db.add_exhibitor(
            company_id, event_id,
            hall=hall,
            stand=stand,
            product_categories=categories if categories else None,
            brands=brands if brands else None,
            detail_url=_clean_str(rd.get("detail_url") or rd.get("source_url")),
            source_url=_clean_str(rd.get("source_url")),
            extra_data=extras if extras else None,
        )
        imported += 1

    db.commit()
    db.log_import("event", event_name, str(filepath), original_rows, imported)
    print(f"    {imported}/{original_rows} rows imported")
    return imported


def import_ifa(db: CrawlerDatabase, base: Path) -> int:
    """Import IFA exhibitors + enrichment data."""
    csv_path = base / "output" / "ifa_exhibitors.csv"
    if not csv_path.exists():
        print("  SKIP: IFA CSV not found")
        return 0

    print("  Importing IFA exhibitors...")
    df = pd.read_csv(csv_path, dtype=str)
    original = len(df)
    df = clean_dataframe(df)
    df = df.dropna(subset=["company_name"])

    # Load enrichment data
    enrich = {}
    enrich_path = base / "output" / "ifa_enrich_progress.json"
    if enrich_path.exists():
        raw = json.loads(enrich_path.read_text(encoding="utf-8"))
        enrich = raw.get("enrichments", {})

    event_id = db.get_or_create_event("IFA", year=2026, city="Berlin", country="Germany")
    imported = 0

    for _, row in df.iterrows():
        rd = row.to_dict()
        name = _clean_str(rd.get("company_name"))
        if not name:
            continue

        slug = _clean_str(rd.get("slug"))

        # Get enrichment for this company
        enriched = enrich.get(slug, {}) if slug else {}

        company_id = db.get_or_create_company(
            name=name,
            slug=slug,
            description=_clean_str(rd.get("description")),
            country=_clean_str(rd.get("country")),
            website=_clean_str(rd.get("website")),
            logo_url=_clean_str(rd.get("logo_url")),
            parent_group=_clean_str(enriched.get("parent_group")),
            vertical=_clean_str(enriched.get("vertical")),
            sub_vertical=_clean_str(enriched.get("sub_vertical")),
            org_size=_clean_str(enriched.get("org_size")),
        )

        # Socials from CSV columns
        for platform in ("linkedin", "instagram", "youtube", "facebook", "twitter"):
            url = _clean_str(rd.get(platform))
            if url:
                db.upsert_social(company_id, platform, url)
        # LinkedIn from enrichment
        linkedin = _clean_str(enriched.get("linkedin_url"))
        if linkedin:
            db.upsert_social(company_id, "linkedin", linkedin)

        # Parse halls/stands
        halls = _clean_str(rd.get("halls"))
        stands = _clean_str(rd.get("stands"))

        extras = {}
        for col in ("show_areas", "events", "competitor_info"):
            val = _clean_str(rd.get(col))
            if val:
                extras[col] = val
        competitor_info = _clean_str(enriched.get("competitor_info"))
        if competitor_info:
            extras["competitor_info"] = competitor_info

        db.add_exhibitor(
            company_id, event_id,
            hall=halls,
            stand=stands,
            detail_url=_clean_str(rd.get("detail_url")),
            extra_data=extras if extras else None,
        )
        imported += 1

    db.commit()
    db.log_import("event", "IFA", str(csv_path), original, imported)
    print(f"    {imported}/{original} rows imported (with enrichment)")
    return imported


def import_ispo(db: CrawlerDatabase, base: Path) -> int:
    """Import ISPO exhibitors — prefer enriched CSV if available."""
    enriched_path = base / "output" / "ispo" / "ispo_exhibitors_2026_enriched.csv"
    plain_path = base / "output" / "ispo" / "ispo_exhibitors_2026.csv"
    csv_path = enriched_path if enriched_path.exists() else plain_path
    if not csv_path.exists():
        print("  SKIP: ISPO CSV not found")
        return 0

    print(f"  Importing ISPO from {csv_path.name}...")
    df = pd.read_csv(csv_path, dtype=str)
    original = len(df)
    df = clean_dataframe(df)
    df = df.dropna(subset=["name"])

    event_id = db.get_or_create_event("ISPO", year=2026, city="Munich", country="Germany")
    imported = 0

    for _, row in df.iterrows():
        rd = row.to_dict()
        name = _clean_str(rd.get("name"))
        if not name:
            continue

        company_id = db.get_or_create_company(
            name=name,
            slug=_clean_str(rd.get("identifier")),
            description=_clean_str(rd.get("biography")),
            website=_clean_str(rd.get("website")),
            email=_clean_str(rd.get("website_email") or rd.get("show_email")),
            phone=_clean_str(rd.get("phone_1")),
            logo_url=_clean_str(rd.get("logo_url")),
            country=_clean_str(rd.get("country_iso") or rd.get("address_1_country")),
            city=_clean_str(rd.get("address_1_city")),
            address=_clean_str(rd.get("address_1_street")),
            postal_code=_clean_str(rd.get("address_1_zip")),
            state=_clean_str(rd.get("address_1_state")),
            employee_count=_clean_int(rd.get("employee_count")),
        )

        facebook = _clean_str(rd.get("social_facebook"))
        if facebook:
            db.upsert_social(company_id, "facebook", facebook)

        db.add_exhibitor(
            company_id, event_id,
            stand=_clean_str(rd.get("stands")),
            detail_url=_clean_str(rd.get("detail_page_url")),
        )

        # Revenue data from enrichment columns
        revenue = _clean_float(rd.get("total_annual_revenue"))
        ecom = _clean_float(rd.get("ecom_revenue"))
        if revenue or ecom:
            db.add_financials(
                company_id,
                year=2026,
                source="ispo_enrichment",
                total_annual_revenue=revenue,
                ecom_revenue=ecom,
                revenue_source=_clean_str(rd.get("revenue_source")),
                confidence=_clean_str(rd.get("confidence")),
            )

        imported += 1

    db.commit()
    db.log_import("event", "ISPO", str(csv_path), original, imported)
    print(f"    {imported}/{original} rows imported")
    return imported


def import_iaw(db: CrawlerDatabase, base: Path) -> int:
    """Import IAW exhibitors."""
    csv_path = base / "output" / "iaw_exhibitors.csv"
    if not csv_path.exists():
        print("  SKIP: IAW CSV not found")
        return 0
    return import_event_csv(
        db, csv_path, "IAW", event_year=2026,
        name_col="name", hall_col=None, stand_col="hall_stand",
        country_col="country", city_col="city",
        website_col="website", email_col="email", phone_col="phone",
    )


def import_modefabriek(db: CrawlerDatabase, base: Path) -> int:
    """Import Modefabriek brands (enriched version preferred)."""
    enriched = base / "output" / "modefabriek_enriched.csv"
    plain = base / "output" / "modefabriek_brands.csv"
    csv_path = enriched if enriched.exists() else plain

    if not csv_path.exists():
        print("  SKIP: Modefabriek CSV not found")
        return 0

    print(f"  Importing Modefabriek from {csv_path.name}...")
    df = pd.read_csv(csv_path, dtype=str)
    original = len(df)
    df = clean_dataframe(df)

    name_col = "brand_name" if "brand_name" in df.columns else "name"
    df = df.dropna(subset=[name_col])

    event_id = db.get_or_create_event("Modefabriek", year=2026, city="Amsterdam", country="Netherlands")
    imported = 0

    for _, row in df.iterrows():
        rd = row.to_dict()
        name = _clean_str(rd.get(name_col))
        if not name:
            continue

        company_id = db.get_or_create_company(
            name=name,
            parent_group=_clean_str(rd.get("parent_group")),
            vertical=_clean_str(rd.get("vertical")),
            sub_vertical=_clean_str(rd.get("sub_vertical")),
            org_size=_clean_str(rd.get("organisation_size")),
        )

        linkedin = _clean_str(rd.get("linkedin_url"))
        if linkedin:
            db.upsert_social(company_id, "linkedin", linkedin)

        db.add_exhibitor(
            company_id, event_id,
            detail_url=_clean_str(rd.get("profile_url")),
        )
        imported += 1

    db.commit()
    db.log_import("event", "Modefabriek", str(csv_path), original, imported)
    print(f"    {imported}/{original} rows imported")
    return imported


# ── Phase 1 raw data importers ─────────────────────────────────────────────

PHASE1_BASE = Path("Database/Raw Data Scrapped/Phase 1_ Raw Data Scrapped")


def import_phase1_events(db: CrawlerDatabase, base: Path) -> int:
    """Import Phase 1 event scraped data."""
    events_dir = base / PHASE1_BASE / "Event Scraped Data"
    total = 0

    # Beauty-Dusseldorf
    p = events_dir / "Beauty-Dusseldorf" / "exhibitors.csv"
    if p.exists():
        total += import_event_csv(
            db, p, "Beauty Dusseldorf", event_year=2026,
            name_col="name", stand_col="booth_or_stand",
        )

    # Eisenwarenmesse
    p = events_dir / "Eisenwarenmesse" / "results.csv"
    if p.exists():
        total += import_event_csv(db, p, "Eisenwarenmesse", event_year=2026)

    # Fibo
    p = events_dir / "Fibo" / "results.csv"
    if p.exists():
        total += import_event_csv(
            db, p, "Fibo", event_year=2026,
            stand_col="extra_stand",
        )

    # Outdoor Trade Show
    p = events_dir / "Outdoor Trade Show" / "results.csv"
    if p.exists():
        total += import_event_csv(
            db, p, "Outdoor Trade Show", event_year=2026,
            stand_col="extra_stand_number",
        )

    # Spogagafa
    p = events_dir / "Spogagafa" / "results.csv"
    if p.exists():
        total += import_event_csv(db, p, "Spogagafa", event_year=2026)

    # Inspired Home (names only)
    p = events_dir / "Inspired Home" / "extract-data-2026-03-06.csv"
    if p.exists():
        print("  Importing Inspired Home (names only)...")
        df = pd.read_csv(p, dtype=str)
        orig = len(df)
        df = df.dropna(subset=["name"])
        event_id = db.get_or_create_event("Inspired Home Show", year=2026)
        count = 0
        for _, row in df.iterrows():
            name = _clean_str(row.get("name"))
            if not name:
                continue
            cid = db.get_or_create_company(name=name)
            db.add_exhibitor(cid, event_id)
            count += 1
        db.commit()
        db.log_import("event", "Inspired Home Show", str(p), orig, count)
        print(f"    {count}/{orig} rows imported")
        total += count

    # Phase 1 xlsx-only events (iaw, ifa, modefabriek — already imported from output/)
    # Skip to avoid duplicates

    return total


def import_marketplace_csv(
    db: CrawlerDatabase,
    filepath: Path,
    marketplace_name: str,
    marketplace_website: str | None = None,
    name_col: str = "businessName",
) -> int:
    """Generic marketplace seller CSV importer."""
    print(f"  Importing {marketplace_name} from {filepath.name}...")
    df = pd.read_csv(filepath, dtype=str)
    original = len(df)
    df = clean_dataframe(df)

    if name_col not in df.columns:
        print(f"    SKIP: no '{name_col}' column")
        return 0

    df = df.dropna(subset=[name_col])
    # Filter out "seller not found" type entries
    df = df[~df[name_col].str.contains("not found|error|unknown", case=False, na=False)]

    mp_id = db.get_or_create_marketplace(marketplace_name, marketplace_website)
    imported = 0

    for _, row in df.iterrows():
        rd = row.to_dict()
        name = _clean_str(rd.get(name_col))
        if not name or len(name) < 2:
            continue

        # Build company fields
        company_kwargs = {"name": name}
        # Try various address field names
        for field, candidates in {
            "email": ["email"],
            "phone": ["phone"],
            "address": ["address", "registeredAddress"],
            "postal_code": ["zipCode"],
            "city": ["city"],
        }.items():
            for c in candidates:
                val = _clean_str(rd.get(c))
                if val:
                    company_kwargs[field] = val
                    break

        company_id = db.get_or_create_company(**company_kwargs)

        db.add_seller(
            company_id, mp_id,
            seller_id=_clean_str(rd.get("sellerId")),
            rating=_clean_float(rd.get("rating")),
            rating_out_of=_clean_float(rd.get("ratingOutOf")),
            review_count=_clean_int(rd.get("reviewCount")),
            vat_number=_clean_str(rd.get("vatNumber")),
            kvk_number=_clean_str(rd.get("kvkNumber")),
            shipped_from=_clean_str(rd.get("shippedFrom")),
            source_url=_clean_str(rd.get("sourceUrl")),
        )
        imported += 1

    db.commit()
    db.log_import("marketplace", marketplace_name, str(filepath), original, imported)
    print(f"    {imported}/{original} rows imported")
    return imported


def import_wayfair(db: CrawlerDatabase, base: Path) -> int:
    """Import all Wayfair A-Z CSVs."""
    wayfair_dir = base / PHASE1_BASE / "WayFair Scraped Data" / "CSV"
    if not wayfair_dir.exists():
        print("  SKIP: Wayfair CSV dir not found")
        return 0

    print("  Importing Wayfair (A-Z)...")
    mp_id = db.get_or_create_marketplace("Wayfair", "https://www.wayfair.com")
    total_imported = 0
    total_rows = 0

    for csv_file in sorted(wayfair_dir.glob("wayfair_api_*.csv")):
        df = pd.read_csv(csv_file, dtype=str)
        total_rows += len(df)
        df = clean_dataframe(df)
        if "name" not in df.columns:
            continue
        df = df.dropna(subset=["name"])

        for _, row in df.iterrows():
            rd = row.to_dict()
            name = _clean_str(rd.get("name"))
            if not name or len(name) < 2:
                continue

            company_id = db.get_or_create_company(
                name=name,
                website=_clean_str(rd.get("url")),
            )
            db.add_seller(company_id, mp_id)
            total_imported += 1

    db.commit()
    db.log_import("marketplace", "Wayfair", str(wayfair_dir), total_rows, total_imported)
    print(f"    {total_imported}/{total_rows} rows imported across all files")
    return total_imported


# ── ECDB importers ──────────────────────────────────────────────────────────

ECDB_BASE = Path("Database/Ecommerce_DB (ECDB) Scraped Data/Ecommerce_DB (ECDB) Scraped Data")


def import_ecdb_companies(db: CrawlerDatabase, base: Path) -> int:
    """Import ECDB Company Ranking."""
    p = base / ECDB_BASE / "Company Ranking.xlsx"
    if not p.exists():
        print("  SKIP: ECDB Company Ranking not found")
        return 0

    print("  Importing ECDB Company Ranking...")
    df = pd.read_excel(p, dtype=str)
    original = len(df)
    df = clean_dataframe(df)
    df = df.dropna(subset=["name"])
    imported = 0

    for _, row in df.iterrows():
        rd = row.to_dict()
        name = _clean_str(rd.get("name"))
        if not name:
            continue

        company_id = db.get_or_create_company(
            name=name,
            slug=_clean_str(rd.get("slug")),
            company_type=_clean_str(rd.get("company_type")),
            country=_clean_str(rd.get("headquarter")),
        )

        db.add_financials(
            company_id,
            year=2025,
            source="ecdb_company_ranking",
            total_gmv_eur=_clean_float(rd.get("total_gmv_eur")),
            rank_global=_clean_int(rd.get("rank_global")),
            rank_filtered=_clean_int(rd.get("rank_filtered")),
            main_country=_clean_str(rd.get("main_country")),
            main_category=_clean_str(rd.get("main_category")),
            extra_data={
                k: rd[k] for k in ("num_subsidiaries", "subsidiaries", "filtered_gmv_eur",
                                     "filtered_share", "main_country_share", "main_category_share")
                if k in rd and _clean_str(rd.get(k))
            } or None,
        )
        imported += 1

    db.commit()
    db.log_import("ecdb", "Company Ranking", str(p), original, imported)
    print(f"    {imported}/{original} rows imported")
    return imported


def import_ecdb_contacts(db: CrawlerDatabase, base: Path) -> int:
    """Import ECDB Contacts and Leads."""
    p = base / ECDB_BASE / "Contacts and Leads.xlsx"
    if not p.exists():
        print("  SKIP: ECDB Contacts not found")
        return 0

    print("  Importing ECDB Contacts & Leads...")
    df = pd.read_excel(p, dtype=str)
    original = len(df)
    df = clean_dataframe(df)
    imported = 0

    for _, row in df.iterrows():
        rd = row.to_dict()
        first = _clean_str(rd.get("first_name"))
        last = _clean_str(rd.get("last_name"))
        if not first and not last:
            continue

        # Try to link to company
        company_id = None
        company_name = _clean_str(rd.get("company_name") or rd.get("store_name"))
        if company_name:
            try:
                company_id = db.get_or_create_company(
                    name=company_name,
                    company_type=_clean_str(rd.get("company_type")),
                    country=_clean_str(rd.get("company_hq_country")),
                    city=_clean_str(rd.get("company_hq_city")),
                    address=_clean_str(rd.get("company_hq_address_line")),
                    postal_code=_clean_str(rd.get("company_hq_post_code")),
                    state=_clean_str(rd.get("company_hq_state")),
                    employee_count=_clean_int(rd.get("headcount")),
                )
                linkedin = _clean_str(rd.get("company_linkedin_url"))
                if linkedin and company_id:
                    db.upsert_social(company_id, "linkedin", linkedin)
            except Exception:
                pass

        db.add_contact(
            company_id,
            first_name=first,
            last_name=last,
            email=_clean_str(rd.get("email")),
            job_title=_clean_str(rd.get("job_title")),
            seniority=_clean_str(rd.get("seniority")),
            department=_clean_str(rd.get("department")),
            linkedin_url=_clean_str(rd.get("personal_linkedin_url")),
            mobile=_clean_str(rd.get("mobile_tel")),
            direct_tel=_clean_str(rd.get("direct_tel")),
            office_tel=_clean_str(rd.get("office_tel")),
            source="ecdb",
        )
        imported += 1

    db.commit()
    db.log_import("ecdb", "Contacts and Leads", str(p), original, imported)
    print(f"    {imported}/{original} rows imported")
    return imported


def import_ecdb_retailers(db: CrawlerDatabase, base: Path) -> int:
    """Import ECDB Retailers Detail Profiles."""
    p = base / ECDB_BASE / "Retailers Detail Profiles.xlsx"
    if not p.exists():
        print("  SKIP: ECDB Retailers Detail Profiles not found")
        return 0

    print("  Importing ECDB Retailers Detail Profiles...")
    df = pd.read_excel(p, dtype=str)
    original = len(df)
    df = clean_dataframe(df)

    name_col = "name" if "name" in df.columns else df.columns[1] if len(df.columns) > 1 else None
    if not name_col:
        print("    SKIP: no name column")
        return 0
    df = df.dropna(subset=[name_col])
    imported = 0

    for _, row in df.iterrows():
        rd = row.to_dict()
        name = _clean_str(rd.get(name_col))
        if not name:
            continue

        company_id = db.get_or_create_company(
            name=name,
            slug=_clean_str(rd.get("slug")),
            company_type=_clean_str(rd.get("store_type")),
        )

        # Pack detailed metrics into financials
        extra = {}
        detail_fields = (
            "num_stores_2025", "top_stores",
            "revenue_net_1p_2024_eur", "revenue_gross_3p_2024_eur",
            "vat_1p_2025_eur", "orders_1p_2025", "orders_3p_2025",
            "aov_net_eur", "aov_discounts_eur", "aov_returns_eur",
            "purchase_frequency_2025", "purchase_frequency_2024",
            "category_1_name", "category_1_share", "category_2_name",
            "country_1_code", "country_1_share", "country_2_code",
        )
        for f in detail_fields:
            val = _clean_str(rd.get(f))
            if val:
                extra[f] = val

        db.add_financials(
            company_id,
            year=2025,
            source="ecdb_retailer_profile",
            total_gmv_eur=_clean_float(rd.get("gmv_2025_eur")),
            net_sales_1p_eur=_clean_float(rd.get("revenue_net_1p_2025_eur")),
            gmv_3p_eur=_clean_float(rd.get("revenue_gross_3p_2025_eur")),
            growth_yoy=_clean_float(rd.get("gmv_growth_yoy")),
            buyers=_clean_int(rd.get("buyers_2025")),
            orders_total=_clean_int(rd.get("orders_total_2025")),
            aov_eur=_clean_float(rd.get("aov_gross_2025_eur")),
            conversion_rate=_clean_float(rd.get("conversion_rate_2025")),
            extra_data=extra if extra else None,
        )
        imported += 1

    db.commit()
    db.log_import("ecdb", "Retailers Detail Profiles", str(p), original, imported)
    print(f"    {imported}/{original} rows imported")
    return imported


def import_ecdb_store_ranking(db: CrawlerDatabase, base: Path) -> int:
    """Import ECDB Store Ranking."""
    p = base / ECDB_BASE / "Store Ranking.xlsx"
    if not p.exists():
        print("  SKIP: ECDB Store Ranking not found")
        return 0

    print("  Importing ECDB Store Ranking...")
    df = pd.read_excel(p, dtype=str)
    original = len(df)
    df = clean_dataframe(df)

    store_col = "store_name" if "store_name" in df.columns else "name"
    if store_col not in df.columns:
        print("    SKIP: no store_name column")
        return 0
    df = df.dropna(subset=[store_col])
    imported = 0

    for _, row in df.iterrows():
        rd = row.to_dict()
        store = _clean_str(rd.get(store_col))
        if not store:
            continue

        # Link to company if possible
        company_name = _clean_str(rd.get("company_name"))
        company_id = None
        if company_name:
            try:
                company_id = db.get_or_create_company(name=company_name)
            except Exception:
                pass

        db.add_store_metric(
            company_id, store,
            slug=_clean_str(rd.get("slug")),
            year=2025,
            total_gmv_eur=_clean_float(rd.get("filtered_total_gmv_eur") or rd.get("global_total_gmv_eur")),
            net_sales_1p=_clean_float(rd.get("global_1p_net_sales_eur")),
            gmv_3p=_clean_float(rd.get("global_3p_gmv_eur")),
            growth_yoy=_clean_float(rd.get("growth_yoy")),
            main_country=_clean_str(rd.get("main_country")),
            main_category=_clean_str(rd.get("main_category")),
            rank_global=_clean_int(rd.get("rank_global")),
            rank_filtered=_clean_int(rd.get("rank_filtered")),
        )
        imported += 1

    db.commit()
    db.log_import("ecdb", "Store Ranking", str(p), original, imported)
    print(f"    {imported}/{original} rows imported")
    return imported


def import_ecdb_retailers_ranking(db: CrawlerDatabase, base: Path) -> int:
    """Import ECDB Retailers Ranking (lighter than profiles — rank + GMV)."""
    p = base / ECDB_BASE / "Retailers Ranking.xlsx"
    if not p.exists():
        print("  SKIP: ECDB Retailers Ranking not found")
        return 0

    print("  Importing ECDB Retailers Ranking...")
    df = pd.read_excel(p, dtype=str)
    original = len(df)
    df = clean_dataframe(df)
    if "name" not in df.columns:
        print("    SKIP: no name column")
        return 0
    df = df.dropna(subset=["name"])
    imported = 0

    for _, row in df.iterrows():
        rd = row.to_dict()
        name = _clean_str(rd.get("name"))
        if not name:
            continue

        company_id = db.get_or_create_company(
            name=name,
            slug=_clean_str(rd.get("slug")),
        )

        db.add_financials(
            company_id,
            year=2025,
            source="ecdb_retailer_ranking",
            total_gmv_eur=_clean_float(rd.get("filtered_total_gmv_eur") or rd.get("global_total_gmv_eur")),
            growth_yoy=_clean_float(rd.get("growth_yoy")),
            rank_global=_clean_int(rd.get("rank_global")),
            rank_filtered=_clean_int(rd.get("rank_filtered")),
            main_country=_clean_str(rd.get("main_country")),
            main_category=_clean_str(rd.get("main_category")),
        )
        imported += 1

    db.commit()
    db.log_import("ecdb", "Retailers Ranking", str(p), original, imported)
    print(f"    {imported}/{original} rows imported")
    return imported


def import_ecdb_reference_rankings(db: CrawlerDatabase, base: Path) -> int:
    """Import Country and Category rankings as reference data."""
    total = 0

    # Country Ranking
    p = base / ECDB_BASE / "Country Ranking.xlsx"
    if p.exists():
        print("  Importing ECDB Country Ranking...")
        df = pd.read_excel(p, dtype=str)
        original = len(df)
        df = clean_dataframe(df)
        name_col = "country_name" if "country_name" in df.columns else "name"
        if name_col in df.columns:
            for _, row in df.iterrows():
                rd = row.to_dict()
                name = _clean_str(rd.get(name_col))
                if not name:
                    continue
                extra = {
                    k: rd[k] for k in ("continent", "country_code", "ecommerce_revenue_eur",
                                        "online_share", "main_category", "main_category_share")
                    if k in rd and _clean_str(rd.get(k))
                }
                db.add_reference_ranking(
                    "country", name, year=2025,
                    rank_global=_clean_int(rd.get("rank_global")),
                    rank_filtered=_clean_int(rd.get("rank_filtered")),
                    revenue_eur=_clean_float(rd.get("filtered_revenue_eur")),
                    growth_yoy=_clean_float(rd.get("growth_yoy")),
                    extra_data=extra if extra else None,
                )
                total += 1
            db.commit()
            db.log_import("ecdb", "Country Ranking", str(p), original, total)
            print(f"    {total}/{original} rows imported")

    # Category Ranking
    p = base / ECDB_BASE / "Product Category Ranking.xlsx"
    if p.exists():
        print("  Importing ECDB Product Category Ranking...")
        df = pd.read_excel(p, dtype=str)
        original = len(df)
        df = clean_dataframe(df)
        name_col = "category_name" if "category_name" in df.columns else "name"
        cat_count = 0
        if name_col in df.columns:
            for _, row in df.iterrows():
                rd = row.to_dict()
                name = _clean_str(rd.get(name_col))
                if not name:
                    continue
                extra = {
                    k: rd[k] for k in ("category_slug", "main_category_name", "region",
                                        "ecommerce_revenue_eur", "online_share")
                    if k in rd and _clean_str(rd.get(k))
                }
                db.add_reference_ranking(
                    "category", name, year=2025,
                    rank_global=_clean_int(rd.get("rank_global")),
                    rank_filtered=_clean_int(rd.get("rank_filtered")),
                    revenue_eur=_clean_float(rd.get("filtered_revenue_eur")),
                    growth_yoy=_clean_float(rd.get("growth_yoy")),
                    extra_data=extra if extra else None,
                )
                cat_count += 1
            db.commit()
            db.log_import("ecdb", "Product Category Ranking", str(p), original, cat_count)
            print(f"    {cat_count}/{original} rows imported")
            total += cat_count

    return total


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import scraped data into SQLite database")
    parser.add_argument("--db-path", default="data/crawler.db", help="Path to SQLite database")
    args = parser.parse_args()

    base = PROJECT_ROOT

    db = CrawlerDatabase(args.db_path)
    db.init_schema()

    print("=" * 60)
    print("IMPORTING DATA INTO DATABASE")
    print("=" * 60)

    # ── 1. Trade Fair / Event data (output/) ──
    print("\n--- Trade Fair / Event Exhibitors ---")
    import_ifa(db, base)
    import_ispo(db, base)
    import_iaw(db, base)
    import_modefabriek(db, base)

    # ── 2. Phase 1 Event data (Database/) ──
    print("\n--- Phase 1 Event Data ---")
    import_phase1_events(db, base)

    # ── 3. Marketplace sellers (Database/) ──
    print("\n--- Marketplace Sellers ---")
    bq_path = base / PHASE1_BASE / "B&Q Scraped Data" / "sellers.csv"
    if bq_path.exists():
        import_marketplace_csv(db, bq_path, "B&Q", "https://www.diy.com")

    mm_path = base / PHASE1_BASE / "MediaMarkt Scraped Data" / "sellers.csv"
    if mm_path.exists():
        import_marketplace_csv(db, mm_path, "MediaMarkt", "https://www.mediamarkt.nl")

    import_wayfair(db, base)

    # ── 4. ECDB data (Database/) ──
    print("\n--- ECDB Data ---")
    import_ecdb_companies(db, base)
    import_ecdb_contacts(db, base)
    import_ecdb_retailers(db, base)
    import_ecdb_retailers_ranking(db, base)
    import_ecdb_store_ranking(db, base)
    import_ecdb_reference_rankings(db, base)

    # ── Summary ──
    print("\n" + "=" * 60)
    print("IMPORT COMPLETE — Database Statistics")
    print("=" * 60)
    stats = db.stats()
    for table, count in stats.items():
        print(f"  {table:.<35} {count:>8,}")
    print(f"\n  Database file: {db.db_path.resolve()}")
    print(f"  Database size: {db.db_path.stat().st_size / 1024 / 1024:.1f} MB")

    db.close()


if __name__ == "__main__":
    main()
