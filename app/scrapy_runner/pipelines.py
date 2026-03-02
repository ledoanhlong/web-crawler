"""ItemCollectorPipeline — collects spider items and writes to JSON output file."""

from __future__ import annotations

import json


class ItemCollectorPipeline:
    """Collect all yielded items and write them to the output file on spider close."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def process_item(self, item: dict, spider) -> dict:  # noqa: ANN001
        self.items.append(dict(item))
        return item

    def close_spider(self, spider) -> None:  # noqa: ANN001
        output_path = spider.settings.get("ITEM_OUTPUT_PATH", "scrapy_output.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=2)
        spider.logger.info("Wrote %d items to %s", len(self.items), output_path)
