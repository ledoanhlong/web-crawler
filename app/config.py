from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Azure OpenAI
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str = "2025-04-01-preview"
    azure_openai_deployment: str = "gpt-5.2"

    # Azure OpenAI — Vision model (GPT-4o for screenshot-based planning)
    azure_vision_endpoint: str = ""
    azure_vision_api_key: str = ""
    azure_vision_api_version: str = "2025-04-01-preview"
    azure_vision_deployment: str = "gpt-4o"
    use_vision_planning: bool = True   # screenshot fallback when CSS selectors fail

    # Interactive exploration browsing (vision-guided page analysis)
    use_exploration_browsing: bool = False  # opt-in: LLM-guided interactive page exploration
    exploration_max_steps: int = 5          # max vision-guided actions per exploration
    exploration_auto_for_js: bool = True    # auto-enable exploration for JS-heavy sites

    # Azure AI Foundry — Claude Opus 4.6 (complex site extraction)
    azure_claude_endpoint: str = ""
    azure_claude_api_key: str = ""
    azure_claude_deployment: str = "claude-opus-4-6"
    use_claude_extraction: bool = True  # full-page LLM fallback for hard sites
    use_script_extraction: bool = True  # generate BS4 extraction scripts with Claude
    allow_generated_script_execution: bool = False
    # Claude circuit breaker
    claude_circuit_breaker_enabled: bool = True
    claude_circuit_breaker_max_errors: int = 3
    claude_circuit_breaker_cooldown_s: int = 600
    # Cost-control policy
    claude_fallback_only: bool = True
    claude_max_retries_per_stage: int = 1
    # Approximate pricing (USD per 1M tokens) for telemetry estimates
    openai_input_cost_per_mtok: float = 2.5
    openai_output_cost_per_mtok: float = 10.0
    claude_input_cost_per_mtok: float = 15.0
    claude_output_cost_per_mtok: float = 75.0

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
    stealth_randomize_viewport: bool = True
    stealth_randomize_user_agent: bool = True
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

    # Job timeout (cooperative — signals graceful shutdown, does not kill the job)
    max_job_duration_s: int = 7200  # 2 hours

    # Playwright
    playwright_headless: bool = True
    playwright_ws_endpoint: str | None = None

    # Output
    output_dir: str = "./output"

    # Crawl4AI (local async crawler with markdown output)
    use_crawl4ai: bool = False                   # master feature flag
    use_crawl4ai_for_fetching: bool = True       # use as page fetcher (markdown output)
    use_crawl4ai_for_extraction: bool = False    # use built-in extraction strategies
    crawl4ai_browser_headless: bool = True

    # universal-scraper (AI-powered BS4 code generation + caching)
    use_universal_scraper: bool = False                # master feature flag
    use_universal_scraper_for_extraction: bool = True  # use for structured extraction
    universal_scraper_model: str = "azure/gpt-5.2"     # LiteLLM model name

    # Shadow-DOM / advanced fallbacks
    use_inner_text_fallback: bool = True          # JS innerText when markdown/CSS fail
    use_listing_api_interception: bool = True     # intercept XHR listing APIs during page load

    # Reliability controls
    reliability_auto_switch_enabled: bool = True
    reliability_auto_switch_min_pages: int = 3
    reliability_auto_switch_zero_streak: int = 2
    reliability_preview_margin_threshold: float = 1.5
    reliability_selector_min_hit_ratio: float = 0.2
    reliability_selector_sample_containers: int = 10
    reliability_quality_min_score: float = 0.0
    reliability_quality_enforce: bool = False

    # CORS / frontend
    allowed_origins: list[str] = ["*"]
    frontend_url: str = ""

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
