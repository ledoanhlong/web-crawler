from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Azure OpenAI
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str = "2025-04-01-preview"
    azure_openai_deployment: str = "gpt-5.2"

    # Crawler
    max_concurrent_requests: int = 5
    request_delay_ms: int = 1000
    request_timeout_s: int = 30
    max_pages_per_crawl: int = 500
    max_sub_links_per_detail: int = 3

    # HTTP retry
    http_max_retries: int = 3
    http_backoff_factor: float = 1.0
    http_retry_status_codes: list[int] = [429, 500, 502, 503, 504, 408]

    # Rate limiting
    min_request_delay_ms: int = 200
    max_request_delay_ms: int = 30_000

    # Stealth / anti-detection
    stealth_enabled: bool = True
    proxy_urls: list[str] = []

    # Sitemap & robots.txt
    use_sitemap_discovery: bool = True
    respect_robots_txt: bool = True

    # Page caching (incremental crawling)
    enable_page_cache: bool = False
    page_cache_max_age_hours: int = 24

    # ScrapeGraphAI — when True, SmartScraperGraph is primary extractor (CSS selectors as backup)
    use_smart_scraper_primary: bool = True

    # Scrapy integration — when True, use Scrapy for static (non-JS) scraping instead of httpx
    use_scrapy: bool = False
    scrapy_concurrent_requests: int = 8
    scrapy_concurrent_requests_per_domain: int = 4
    scrapy_download_delay: float = 0.5
    scrapy_randomize_delay: bool = True
    scrapy_retry_times: int = 3
    scrapy_retry_http_codes: list[int] = [500, 502, 503, 504, 408, 429]
    scrapy_obey_robotstxt: bool = True
    scrapy_autothrottle_enabled: bool = True
    scrapy_autothrottle_start_delay: float = 1.0
    scrapy_autothrottle_max_delay: float = 10.0
    scrapy_autothrottle_target_concurrency: float = 2.0
    scrapy_subprocess_timeout_s: int = 600

    # Playwright
    playwright_headless: bool = True
    playwright_ws_endpoint: str | None = None

    # Output
    output_dir: str = "./output"

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
