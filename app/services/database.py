"""Database service — SQLite backend for storing all scraped data.

Provides schema creation, company deduplication, and insert helpers
used by both the bulk import script and the live crawler pipeline.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.utils.logging import get_logger

log = get_logger(__name__)

try:
    from app.config import settings as _settings
    DB_PATH = Path(_settings.database_path)
except Exception:
    DB_PATH = Path("./data/crawler.db")

# ── Schema ──────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Master company table (deduplicated)
CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    name_normalized TEXT NOT NULL,          -- lowercase, stripped, for dedup
    slug            TEXT,
    company_type    TEXT,                   -- retailer, brand, manufacturer, etc.
    description     TEXT,
    country         TEXT,
    city            TEXT,
    address         TEXT,
    postal_code     TEXT,
    state           TEXT,
    website         TEXT,
    email           TEXT,
    phone           TEXT,
    fax             TEXT,
    logo_url        TEXT,
    parent_group    TEXT,
    vertical        TEXT,
    sub_vertical    TEXT,
    org_size        TEXT,
    employee_count  INTEGER,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_name_norm
    ON companies(name_normalized);
CREATE INDEX IF NOT EXISTS idx_companies_country
    ON companies(country);
CREATE INDEX IF NOT EXISTS idx_companies_website
    ON companies(website);

-- Social media links (one row per platform per company)
CREATE TABLE IF NOT EXISTS company_socials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL,
    url             TEXT NOT NULL,
    UNIQUE(company_id, platform)
);

-- Individual contacts / leads
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    first_name      TEXT,
    last_name       TEXT,
    email           TEXT,
    job_title       TEXT,
    seniority       TEXT,
    department      TEXT,
    linkedin_url    TEXT,
    mobile          TEXT,
    direct_tel      TEXT,
    office_tel      TEXT,
    source          TEXT,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_contacts_company
    ON contacts(company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email
    ON contacts(email);

-- Trade fair / event definitions
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    year            INTEGER,
    city            TEXT,
    country         TEXT,
    website         TEXT,
    UNIQUE(name_normalized, year)
);

-- Company <-> Event (many-to-many)
CREATE TABLE IF NOT EXISTS event_exhibitors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    event_id        INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    hall            TEXT,
    stand           TEXT,
    booth           TEXT,
    product_categories TEXT,               -- JSON array
    brands          TEXT,                  -- JSON array
    detail_url      TEXT,
    source_url      TEXT,
    extra_data      TEXT,                  -- JSON object for sparse extra_* columns
    UNIQUE(company_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_exhibitors_event
    ON event_exhibitors(event_id);

-- Marketplace definitions
CREATE TABLE IF NOT EXISTS marketplaces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    website         TEXT
);

-- Company <-> Marketplace (many-to-many)
CREATE TABLE IF NOT EXISTS marketplace_sellers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    marketplace_id  INTEGER NOT NULL REFERENCES marketplaces(id) ON DELETE CASCADE,
    seller_id       TEXT,
    rating          REAL,
    rating_out_of   REAL,
    review_count    INTEGER,
    vat_number      TEXT,
    kvk_number      TEXT,
    shipped_from    TEXT,
    source_url      TEXT,
    extra_data      TEXT,
    UNIQUE(company_id, marketplace_id)
);
CREATE INDEX IF NOT EXISTS idx_sellers_marketplace
    ON marketplace_sellers(marketplace_id);

-- Financial / revenue data
CREATE TABLE IF NOT EXISTS company_financials (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id           INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    year                 INTEGER,
    source               TEXT,
    total_gmv_eur        REAL,
    net_sales_1p_eur     REAL,
    gmv_3p_eur           REAL,
    growth_yoy           REAL,
    total_annual_revenue REAL,
    ecom_revenue         REAL,
    revenue_source       TEXT,
    confidence           TEXT,
    buyers               INTEGER,
    orders_total         INTEGER,
    aov_eur              REAL,
    conversion_rate      REAL,
    rank_global          INTEGER,
    rank_filtered        INTEGER,
    main_country         TEXT,
    main_category        TEXT,
    extra_data           TEXT,
    UNIQUE(company_id, year, source)
);

-- Store-level metrics (from ECDB)
CREATE TABLE IF NOT EXISTS store_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id      INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    store_name      TEXT NOT NULL,
    slug            TEXT,
    year            INTEGER,
    total_gmv_eur   REAL,
    net_sales_1p    REAL,
    gmv_3p          REAL,
    growth_yoy      REAL,
    main_country    TEXT,
    main_category   TEXT,
    rank_global     INTEGER,
    rank_filtered   INTEGER,
    extra_data      TEXT
);
CREATE INDEX IF NOT EXISTS idx_store_company
    ON store_metrics(company_id);

-- Country / category reference rankings
CREATE TABLE IF NOT EXISTS reference_rankings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ranking_type    TEXT NOT NULL,          -- 'country' or 'category'
    name            TEXT NOT NULL,
    rank_global     INTEGER,
    rank_filtered   INTEGER,
    revenue_eur     REAL,
    growth_yoy      REAL,
    year            INTEGER,
    extra_data      TEXT,
    UNIQUE(ranking_type, name, year)
);

-- Import provenance log
CREATE TABLE IF NOT EXISTS scrape_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type     TEXT NOT NULL,          -- 'event', 'marketplace', 'ecdb', 'crawler'
    source_name     TEXT NOT NULL,
    file_path       TEXT,
    row_count       INTEGER,
    rows_imported   INTEGER,
    scraped_at      TEXT,
    imported_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    notes           TEXT
);
"""


# ── Helpers ─────────────────────────────────────────────────────────────────

def normalize_name(name: str | None) -> str:
    """Lowercase, strip whitespace/punctuation for dedup matching."""
    if not name:
        return ""
    s = name.strip().lower()
    # collapse multiple spaces
    s = re.sub(r"\s+", " ", s)
    return s


def _clean_str(val: Any) -> str | None:
    """Return None for empty / whitespace-only / 'nan' values."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "n/a", "null", ""):
        return None
    return s


def _clean_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        if f != f:  # NaN check
            return None
        return f
    except (ValueError, TypeError):
        return None


def _clean_int(val: Any) -> int | None:
    f = _clean_float(val)
    if f is None:
        return None
    return int(f)


def _to_json(val: Any) -> str | None:
    """Serialize to JSON string, or None for empty."""
    if val is None:
        return None
    if isinstance(val, str):
        val = val.strip()
        if not val or val.lower() in ("nan", "none"):
            return None
        return val
    return json.dumps(val, ensure_ascii=False)


# ── Database class ──────────────────────────────────────────────────────────

class CrawlerDatabase:
    """SQLite database for all crawler scraped data."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    def init_schema(self) -> None:
        """Create all tables and indexes."""
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
        log.info("Database schema initialized at %s", self.db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Company dedup/upsert ────────────────────────────────────────────

    def get_or_create_company(
        self,
        name: str,
        *,
        slug: str | None = None,
        company_type: str | None = None,
        description: str | None = None,
        country: str | None = None,
        city: str | None = None,
        address: str | None = None,
        postal_code: str | None = None,
        state: str | None = None,
        website: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        fax: str | None = None,
        logo_url: str | None = None,
        parent_group: str | None = None,
        vertical: str | None = None,
        sub_vertical: str | None = None,
        org_size: str | None = None,
        employee_count: int | None = None,
    ) -> int:
        """Find existing company by normalized name, or create a new one.

        If found, merges non-null fields into existing record (fill gaps, don't overwrite).
        Returns company ID.
        """
        name_norm = normalize_name(name)
        if not name_norm:
            raise ValueError("Company name cannot be empty")

        row = self.conn.execute(
            "SELECT id FROM companies WHERE name_normalized = ?",
            (name_norm,),
        ).fetchone()

        now = datetime.now(timezone.utc).isoformat()

        if row:
            company_id = row[0]
            # Merge: fill NULLs in existing record with new data
            updates = {}
            fields = {
                "slug": slug, "company_type": company_type,
                "description": description, "country": country, "city": city,
                "address": address, "postal_code": postal_code, "state": state,
                "website": website, "email": email, "phone": phone, "fax": fax,
                "logo_url": logo_url, "parent_group": parent_group,
                "vertical": vertical, "sub_vertical": sub_vertical,
                "org_size": org_size, "employee_count": employee_count,
            }
            for col, val in fields.items():
                if val is not None:
                    updates[col] = val

            if updates:
                # Only fill NULLs — don't overwrite existing values
                set_clauses = [
                    f"{col} = COALESCE({col}, ?)" for col in updates
                ]
                set_clauses.append("updated_at = ?")
                sql = f"UPDATE companies SET {', '.join(set_clauses)} WHERE id = ?"
                params = list(updates.values()) + [now, company_id]
                self.conn.execute(sql, params)

            return company_id

        # Create new
        self.conn.execute(
            """INSERT INTO companies (
                name, name_normalized, slug, company_type, description,
                country, city, address, postal_code, state,
                website, email, phone, fax, logo_url,
                parent_group, vertical, sub_vertical, org_size, employee_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, name_norm, slug, company_type, description,
                country, city, address, postal_code, state,
                website, email, phone, fax, logo_url,
                parent_group, vertical, sub_vertical, org_size, employee_count,
                now, now,
            ),
        )
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # ── Social media ────────────────────────────────────────────────────

    def upsert_social(self, company_id: int, platform: str, url: str) -> None:
        """Insert or update a social media link for a company."""
        platform = platform.strip().lower()
        url = url.strip()
        if not url or url.lower() in ("nan", "none"):
            return
        self.conn.execute(
            """INSERT INTO company_socials (company_id, platform, url)
               VALUES (?, ?, ?)
               ON CONFLICT(company_id, platform) DO UPDATE SET url = excluded.url""",
            (company_id, platform, url),
        )

    # ── Events ──────────────────────────────────────────────────────────

    def get_or_create_event(
        self,
        name: str,
        year: int | None = None,
        city: str | None = None,
        country: str | None = None,
        website: str | None = None,
    ) -> int:
        name_norm = normalize_name(name)
        row = self.conn.execute(
            "SELECT id FROM events WHERE name_normalized = ? AND (year = ? OR year IS NULL)",
            (name_norm, year),
        ).fetchone()
        if row:
            return row[0]
        self.conn.execute(
            "INSERT INTO events (name, name_normalized, year, city, country, website) VALUES (?, ?, ?, ?, ?, ?)",
            (name, name_norm, year, city, country, website),
        )
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def add_exhibitor(
        self,
        company_id: int,
        event_id: int,
        *,
        hall: str | None = None,
        stand: str | None = None,
        booth: str | None = None,
        product_categories: list[str] | None = None,
        brands: list[str] | None = None,
        detail_url: str | None = None,
        source_url: str | None = None,
        extra_data: dict | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO event_exhibitors
               (company_id, event_id, hall, stand, booth,
                product_categories, brands, detail_url, source_url, extra_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(company_id, event_id) DO UPDATE SET
                   hall = COALESCE(excluded.hall, hall),
                   stand = COALESCE(excluded.stand, stand),
                   booth = COALESCE(excluded.booth, booth),
                   product_categories = COALESCE(excluded.product_categories, product_categories),
                   brands = COALESCE(excluded.brands, brands),
                   detail_url = COALESCE(excluded.detail_url, detail_url),
                   source_url = COALESCE(excluded.source_url, source_url),
                   extra_data = COALESCE(excluded.extra_data, extra_data)""",
            (
                company_id, event_id, hall, stand, booth,
                _to_json(product_categories), _to_json(brands),
                detail_url, source_url, _to_json(extra_data),
            ),
        )

    # ── Marketplaces ────────────────────────────────────────────────────

    def get_or_create_marketplace(self, name: str, website: str | None = None) -> int:
        row = self.conn.execute(
            "SELECT id FROM marketplaces WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return row[0]
        self.conn.execute(
            "INSERT INTO marketplaces (name, website) VALUES (?, ?)",
            (name, website),
        )
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def add_seller(
        self,
        company_id: int,
        marketplace_id: int,
        *,
        seller_id: str | None = None,
        rating: float | None = None,
        rating_out_of: float | None = None,
        review_count: int | None = None,
        vat_number: str | None = None,
        kvk_number: str | None = None,
        shipped_from: str | None = None,
        source_url: str | None = None,
        extra_data: dict | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO marketplace_sellers
               (company_id, marketplace_id, seller_id, rating, rating_out_of,
                review_count, vat_number, kvk_number, shipped_from, source_url, extra_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(company_id, marketplace_id) DO UPDATE SET
                   seller_id = COALESCE(excluded.seller_id, seller_id),
                   rating = COALESCE(excluded.rating, rating),
                   review_count = COALESCE(excluded.review_count, review_count),
                   vat_number = COALESCE(excluded.vat_number, vat_number),
                   source_url = COALESCE(excluded.source_url, source_url)""",
            (
                company_id, marketplace_id, seller_id, rating, rating_out_of,
                review_count, vat_number, kvk_number, shipped_from, source_url,
                _to_json(extra_data),
            ),
        )

    # ── Financials ──────────────────────────────────────────────────────

    def add_financials(
        self,
        company_id: int,
        year: int | None = None,
        source: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.conn.execute(
            """INSERT INTO company_financials
               (company_id, year, source, total_gmv_eur, net_sales_1p_eur, gmv_3p_eur,
                growth_yoy, total_annual_revenue, ecom_revenue, revenue_source, confidence,
                buyers, orders_total, aov_eur, conversion_rate,
                rank_global, rank_filtered, main_country, main_category, extra_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(company_id, year, source) DO UPDATE SET
                   total_gmv_eur = COALESCE(excluded.total_gmv_eur, total_gmv_eur),
                   growth_yoy = COALESCE(excluded.growth_yoy, growth_yoy),
                   total_annual_revenue = COALESCE(excluded.total_annual_revenue, total_annual_revenue),
                   rank_global = COALESCE(excluded.rank_global, rank_global)""",
            (
                company_id, year, source,
                _clean_float(kwargs.get("total_gmv_eur")),
                _clean_float(kwargs.get("net_sales_1p_eur")),
                _clean_float(kwargs.get("gmv_3p_eur")),
                _clean_float(kwargs.get("growth_yoy")),
                _clean_float(kwargs.get("total_annual_revenue")),
                _clean_float(kwargs.get("ecom_revenue")),
                _clean_str(kwargs.get("revenue_source")),
                _clean_str(kwargs.get("confidence")),
                _clean_int(kwargs.get("buyers")),
                _clean_int(kwargs.get("orders_total")),
                _clean_float(kwargs.get("aov_eur")),
                _clean_float(kwargs.get("conversion_rate")),
                _clean_int(kwargs.get("rank_global")),
                _clean_int(kwargs.get("rank_filtered")),
                _clean_str(kwargs.get("main_country")),
                _clean_str(kwargs.get("main_category")),
                _to_json(kwargs.get("extra_data")),
            ),
        )

    # ── Store metrics ───────────────────────────────────────────────────

    def add_store_metric(self, company_id: int | None, store_name: str, **kwargs: Any) -> None:
        self.conn.execute(
            """INSERT INTO store_metrics
               (company_id, store_name, slug, year, total_gmv_eur, net_sales_1p, gmv_3p,
                growth_yoy, main_country, main_category, rank_global, rank_filtered, extra_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id, store_name,
                _clean_str(kwargs.get("slug")),
                _clean_int(kwargs.get("year")),
                _clean_float(kwargs.get("total_gmv_eur")),
                _clean_float(kwargs.get("net_sales_1p")),
                _clean_float(kwargs.get("gmv_3p")),
                _clean_float(kwargs.get("growth_yoy")),
                _clean_str(kwargs.get("main_country")),
                _clean_str(kwargs.get("main_category")),
                _clean_int(kwargs.get("rank_global")),
                _clean_int(kwargs.get("rank_filtered")),
                _to_json(kwargs.get("extra_data")),
            ),
        )

    # ── Contacts ────────────────────────────────────────────────────────

    def add_contact(self, company_id: int | None, **kwargs: Any) -> int:
        self.conn.execute(
            """INSERT INTO contacts
               (company_id, first_name, last_name, email, job_title,
                seniority, department, linkedin_url, mobile, direct_tel, office_tel, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company_id,
                _clean_str(kwargs.get("first_name")),
                _clean_str(kwargs.get("last_name")),
                _clean_str(kwargs.get("email")),
                _clean_str(kwargs.get("job_title")),
                _clean_str(kwargs.get("seniority")),
                _clean_str(kwargs.get("department")),
                _clean_str(kwargs.get("linkedin_url")),
                _clean_str(kwargs.get("mobile")),
                _clean_str(kwargs.get("direct_tel")),
                _clean_str(kwargs.get("office_tel")),
                _clean_str(kwargs.get("source")),
            ),
        )
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # ── Reference rankings ──────────────────────────────────────────────

    def add_reference_ranking(self, ranking_type: str, name: str, year: int, **kwargs: Any) -> None:
        self.conn.execute(
            """INSERT INTO reference_rankings
               (ranking_type, name, rank_global, rank_filtered, revenue_eur, growth_yoy, year, extra_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ranking_type, name, year) DO UPDATE SET
                   rank_global = COALESCE(excluded.rank_global, rank_global),
                   revenue_eur = COALESCE(excluded.revenue_eur, revenue_eur)""",
            (
                ranking_type, name,
                _clean_int(kwargs.get("rank_global")),
                _clean_int(kwargs.get("rank_filtered")),
                _clean_float(kwargs.get("revenue_eur")),
                _clean_float(kwargs.get("growth_yoy")),
                year,
                _to_json(kwargs.get("extra_data")),
            ),
        )

    # ── Scrape log ──────────────────────────────────────────────────────

    def log_import(
        self,
        source_type: str,
        source_name: str,
        file_path: str | None = None,
        row_count: int | None = None,
        rows_imported: int | None = None,
        scraped_at: str | None = None,
        notes: str | None = None,
    ) -> int:
        self.conn.execute(
            """INSERT INTO scrape_log
               (source_type, source_name, file_path, row_count, rows_imported, scraped_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source_type, source_name, file_path, row_count, rows_imported, scraped_at, notes),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # ── Insert from crawler SellerLead ──────────────────────────────────

    def insert_seller_lead(
        self,
        lead: dict,
        source_type: str = "crawler",
        event_name: str | None = None,
        event_year: int | None = None,
        marketplace_name: str | None = None,
    ) -> int:
        """Insert a SellerLead dict into the database, deduplicating by company name.

        Automatically routes to event_exhibitors or marketplace_sellers based on params.
        Returns the company_id.
        """
        name = _clean_str(lead.get("name"))
        if not name:
            raise ValueError("SellerLead must have a name")

        # Extract social media
        socials = lead.get("social_media", {})
        extra = lead.get("raw_extra", {})

        company_id = self.get_or_create_company(
            name=name,
            description=_clean_str(lead.get("description")),
            country=_clean_str(lead.get("country")),
            city=_clean_str(lead.get("city")),
            address=_clean_str(lead.get("address")),
            postal_code=_clean_str(lead.get("postal_code")),
            website=_clean_str(lead.get("website")),
            email=_clean_str(lead.get("email")),
            phone=_clean_str(lead.get("phone")),
            logo_url=_clean_str(lead.get("logo_url")),
        )

        # Social media
        for platform, url in socials.items():
            url = _clean_str(url)
            if url:
                self.upsert_social(company_id, platform, url)

        # Route to event or marketplace
        if event_name:
            event_id = self.get_or_create_event(event_name, year=event_year)
            self.add_exhibitor(
                company_id, event_id,
                hall=_clean_str(lead.get("hall")),
                stand=_clean_str(lead.get("booth")),
                booth=_clean_str(lead.get("booth")),
                product_categories=lead.get("product_categories"),
                brands=lead.get("brands"),
                source_url=_clean_str(lead.get("source_url")),
                extra_data=extra if extra else None,
            )
        elif marketplace_name:
            mp_name = marketplace_name or _clean_str(lead.get("marketplace_name"))
            if mp_name:
                mp_id = self.get_or_create_marketplace(mp_name)
                self.add_seller(
                    company_id, mp_id,
                    source_url=_clean_str(lead.get("store_url") or lead.get("source_url")),
                    extra_data=extra if extra else None,
                )

        return company_id

    # ── Bulk helpers ────────────────────────────────────────────────────

    def commit(self) -> None:
        self.conn.commit()

    def stats(self) -> dict[str, int]:
        """Return row counts for all tables."""
        tables = [
            "companies", "company_socials", "contacts", "events",
            "event_exhibitors", "marketplaces", "marketplace_sellers",
            "company_financials", "store_metrics", "reference_rankings", "scrape_log",
        ]
        result = {}
        for t in tables:
            row = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            result[t] = row[0]
        return result
