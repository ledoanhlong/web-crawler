"""Subprocess entry point for running the Scrapy spider.

Usage:
    python -m app.scrapy_runner.run --plan <plan.json> --output <output.json> [--max-items N]

This runs in a separate process so that Scrapy's Twisted reactor does not
conflict with the main process's asyncio event loop or Playwright.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Force SelectorEventLoop on Windows before any Twisted import.
# Scrapy/Twisted requires this; ProactorEventLoop is incompatible.
if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from scrapy.crawler import CrawlerProcess  # noqa: E402


def _bool_env(key: str, default: str = "true") -> bool:
    return os.environ.get(key, default).lower() == "true"


def build_scrapy_settings(output_path: str, max_items: int | None) -> dict:
    """Build Scrapy settings from environment variables set by the parent process."""
    s = {
        "BOT_NAME": "web_crawler_agent",
        "SPIDER_MODULES": [],
        "NEWSPIDER_MODULE": "",
        "USER_AGENT": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "ROBOTSTXT_OBEY": _bool_env("SCRAPY_OBEY_ROBOTSTXT"),
        "CONCURRENT_REQUESTS": int(os.environ.get("SCRAPY_CONCURRENT_REQUESTS", "8")),
        "CONCURRENT_REQUESTS_PER_DOMAIN": int(
            os.environ.get("SCRAPY_CONCURRENT_REQUESTS_PER_DOMAIN", "4")
        ),
        "DOWNLOAD_DELAY": float(os.environ.get("SCRAPY_DOWNLOAD_DELAY", "0.5")),
        "RANDOMIZE_DOWNLOAD_DELAY": _bool_env("SCRAPY_RANDOMIZE_DELAY"),
        "COOKIES_ENABLED": True,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": int(os.environ.get("SCRAPY_RETRY_TIMES", "3")),
        "RETRY_HTTP_CODES": json.loads(
            os.environ.get("SCRAPY_RETRY_HTTP_CODES", "[500,502,503,504,408,429]")
        ),
        "DOWNLOAD_TIMEOUT": int(os.environ.get("SCRAPY_DOWNLOAD_TIMEOUT", "30")),
        "AUTOTHROTTLE_ENABLED": _bool_env("SCRAPY_AUTOTHROTTLE_ENABLED"),
        "AUTOTHROTTLE_START_DELAY": float(
            os.environ.get("SCRAPY_AUTOTHROTTLE_START_DELAY", "1.0")
        ),
        "AUTOTHROTTLE_MAX_DELAY": float(
            os.environ.get("SCRAPY_AUTOTHROTTLE_MAX_DELAY", "10.0")
        ),
        "AUTOTHROTTLE_TARGET_CONCURRENCY": float(
            os.environ.get("SCRAPY_AUTOTHROTTLE_TARGET_CONCURRENCY", "2.0")
        ),
        "HTTPCACHE_ENABLED": False,
        "REQUEST_FINGERPRINTER_IMPLEMENTATION": "2.7",
        "LOG_LEVEL": os.environ.get("SCRAPY_LOG_LEVEL", "INFO"),
        "ITEM_PIPELINES": {
            "app.scrapy_runner.pipelines.ItemCollectorPipeline": 300,
        },
        "ITEM_OUTPUT_PATH": output_path,
    }

    if max_items is not None and max_items > 0:
        s["CLOSESPIDER_ITEMCOUNT"] = max_items

    return s


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Scrapy PlanSpider in subprocess")
    parser.add_argument("--plan", required=True, help="Path to plan JSON file")
    parser.add_argument("--output", required=True, help="Path to output JSON file")
    parser.add_argument("--max-items", type=int, default=None, help="Max items to scrape")
    args = parser.parse_args()

    with open(args.plan, "r", encoding="utf-8") as f:
        plan_dict = json.load(f)

    scrapy_settings = build_scrapy_settings(args.output, args.max_items)

    from app.scrapy_runner.spider import PlanSpider

    process = CrawlerProcess(settings=scrapy_settings)
    process.crawl(PlanSpider, plan_dict=plan_dict, max_items=args.max_items)
    process.start()


if __name__ == "__main__":
    main()
