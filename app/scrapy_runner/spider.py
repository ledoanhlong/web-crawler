"""PlanSpider — dynamic Scrapy spider driven by a serialized ScrapingPlan.

Receives the plan as a dict (from JSON) and uses its CSS selectors, pagination
strategy, and detail-link config to crawl listing pages, follow pagination,
and fetch detail pages — all with Scrapy's built-in retry, throttle, and
deduplication features.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import scrapy
from scrapy.http import Response


class PlanSpider(scrapy.Spider):
    name = "plan_spider"

    def __init__(
        self,
        plan_dict: dict,
        max_items: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.plan = plan_dict
        self.max_items = int(max_items) if max_items is not None else None
        self.items_yielded = 0
        self.target = plan_dict["target"]
        self.base_url = plan_dict["url"]
        parsed = urlparse(self.base_url)
        self.base_origin = f"{parsed.scheme}://{parsed.netloc}"

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def start_requests(self):
        pagination = self.plan.get("pagination", "none")
        pagination_urls = self.plan.get("pagination_urls", [])

        if pagination == "page_numbers" and pagination_urls:
            for url in pagination_urls:
                if self._limit_reached():
                    break
                yield scrapy.Request(url, callback=self.parse_listing)
        else:
            yield scrapy.Request(self.base_url, callback=self.parse_listing)

    # ------------------------------------------------------------------
    # Listing page parser
    # ------------------------------------------------------------------
    def parse_listing(self, response: Response):
        container_sel = self.target["item_container_selector"]
        field_selectors = self.target.get("field_selectors", {})
        field_attributes = self.target.get("field_attributes", {})
        detail_link_sel = self.target.get("detail_link_selector")

        containers = response.css(container_sel)
        self.logger.info("Found %d items on %s", len(containers), response.url)

        for container in containers:
            if self._limit_reached():
                return

            record: dict[str, str | None] = {}

            # Extract fields using plan selectors
            for field, selector in field_selectors.items():
                attr = field_attributes.get(field)
                if attr:
                    record[field] = container.css(selector).attrib.get(attr)
                else:
                    # Join all descendant text to match BeautifulSoup get_text()
                    texts = container.css(selector + " ::text").getall()
                    record[field] = (
                        " ".join(t.strip() for t in texts if t.strip()) or None
                    )

            # Extract detail link
            if detail_link_sel:
                link_el = container.css(detail_link_sel)
                if link_el:
                    href = link_el.attrib.get("href", "")
                    if href:
                        record["detail_link"] = response.urljoin(href)

            # Extract API detail ID if detail_api_plan exists
            detail_api_plan = self.plan.get("detail_api_plan")
            if detail_api_plan:
                id_sel = detail_api_plan.get("id_selector")
                if id_sel:
                    id_el = container.css(id_sel)
                    if id_el:
                        id_attr = detail_api_plan.get("id_attribute")
                        if id_attr:
                            raw_id = id_el.attrib.get(id_attr, "")
                        else:
                            raw_id = id_el.css("::text").get(default="").strip()
                        id_regex = detail_api_plan.get("id_regex")
                        if id_regex and raw_id:
                            match = re.search(id_regex, str(raw_id))
                            if match:
                                raw_id = match.group(1)
                        if raw_id:
                            record["_detail_api_id"] = str(raw_id)

            record["_source_url"] = response.url

            # Follow detail page or yield item directly
            if record.get("detail_link") and detail_link_sel:
                yield scrapy.Request(
                    record["detail_link"],
                    callback=self.parse_detail,
                    cb_kwargs={"listing_item": record},
                )
            else:
                self.items_yielded += 1
                yield {"type": "item", **record}

        # Handle next_button pagination
        pagination = self.plan.get("pagination", "none")
        pagination_selector = self.plan.get("pagination_selector")
        if pagination == "next_button" and pagination_selector and not self._limit_reached():
            next_el = response.css(pagination_selector)
            if next_el:
                next_href = next_el.attrib.get("href")
                if next_href:
                    yield scrapy.Request(
                        response.urljoin(next_href),
                        callback=self.parse_listing,
                    )

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------
    def parse_detail(self, response: Response, listing_item: dict):
        if self._limit_reached():
            return

        self.items_yielded += 1
        yield {
            "type": "item",
            **listing_item,
            "_detail_html": response.text,
            "_detail_url": response.url,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _limit_reached(self) -> bool:
        return self.max_items is not None and self.items_yielded >= self.max_items
