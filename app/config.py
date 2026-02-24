from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Azure OpenAI
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str = "2025-04-01-preview"
    azure_openai_deployment: str = "gpt-52"

    # Crawler
    max_concurrent_requests: int = 5
    request_delay_ms: int = 1000
    request_timeout_s: int = 30
    max_pages_per_crawl: int = 500

    # Playwright
    playwright_headless: bool = True
    playwright_ws_endpoint: str | None = None

    # Output
    output_dir: str = "./output"

    # Logging
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
