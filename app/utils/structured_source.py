"""Helpers for embedded structured listing sources."""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.models.schemas import ListingApiPlan

_ATTR_HINTS = (
    "data-info",
    "data-results",
    "data-json",
    "data-state",
    "data-props",
    "data-payload",
    "data-initial",
)

_KEY_HINTS = {
    "name",
    "title",
    "company",
    "exhibitor",
    "seller",
    "vendor",
    "booth",
    "hall",
    "description",
    "website",
    "email",
    "phone",
    "contact",
    "country",
    "city",
    "address",
    "category",
    "brand",
}


@dataclass
class _EmbeddedCandidate:
    plan: ListingApiPlan
    score: int


def _flatten_item(raw: dict) -> dict[str, str | None]:
    flat: dict[str, str | None] = {}

    def _walk(obj: dict[str, Any], prefix: str = "") -> None:
        for key, value in obj.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if value is None:
                flat[full_key] = None
            elif isinstance(value, dict):
                _walk(value, full_key)
            elif isinstance(value, list):
                flat[full_key] = json.dumps(value, ensure_ascii=False)
            else:
                flat[full_key] = str(value)

    _walk(raw)
    return flat


def _parse_json_blob(raw_value: str | None) -> Any:
    if not raw_value:
        return None
    value = html_lib.unescape(raw_value).strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _iter_item_lists(data: Any, path: str | None = None, depth: int = 0) -> list[tuple[list[dict], str | None]]:
    if depth > 4:
        return []

    matches: list[tuple[list[dict], str | None]] = []
    if isinstance(data, list):
        dict_items = [item for item in data if isinstance(item, dict)]
        if len(dict_items) >= 2:
            matches.append((dict_items, path))
        return matches

    if not isinstance(data, dict):
        return matches

    for key, value in data.items():
        next_path = f"{path}.{key}" if path else key
        if isinstance(value, list):
            dict_items = [item for item in value if isinstance(item, dict)]
            if len(dict_items) >= 2:
                matches.append((dict_items, next_path))
        elif isinstance(value, dict):
            matches.extend(_iter_item_lists(value, next_path, depth + 1))

    return matches


def _score_items(items: list[dict], *, source_hint: str = "", path: str | None = None) -> int:
    sample = items[:5]
    hits = 0
    for item in sample:
        keys: set[str] = set()
        for key, value in item.items():
            keys.add(str(key).lower())
            if isinstance(value, dict):
                keys.update(str(inner_key).lower() for inner_key in value.keys())
        hits += sum(1 for key in keys if any(hint in key for hint in _KEY_HINTS))

    score = hits * 10 + min(len(items), 100)
    hint_text = f"{source_hint} {path or ''}".lower()
    if "result" in hint_text:
        score += 20
    if "item" in hint_text:
        score += 10
    if "exhibitor" in hint_text or "seller" in hint_text or "vendor" in hint_text:
        score += 20
    return score


def _selector_for_element(element: Tag, attr_name: str | None = None) -> str:
    tag_name = element.name or "*"
    element_id = element.get("id")
    if isinstance(element_id, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", element_id):
        return f"{tag_name}#{element_id}"

    classes = [
        cls for cls in element.get("class", [])
        if isinstance(cls, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", cls)
    ]
    if classes:
        selector = tag_name + "".join(f".{cls}" for cls in classes[:3])
    else:
        selector = tag_name
    if attr_name:
        selector += f"[{attr_name}]"
    return selector


def _build_candidate(
    *,
    source_url: str,
    element: Tag,
    attr_name: str | None,
    data: Any,
    source_hint: str,
) -> _EmbeddedCandidate | None:
    item_lists = _iter_item_lists(data)
    if isinstance(data, list):
        dict_items = [item for item in data if isinstance(item, dict)]
        if len(dict_items) >= 2:
            item_lists.insert(0, (dict_items, None))

    if not item_lists:
        return None

    scored = sorted(
        (
            (_score_items(items, source_hint=source_hint, path=path), items, path)
            for items, path in item_lists
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best_items, best_path = scored[0]
    if best_score < 30:
        return None

    return _EmbeddedCandidate(
        plan=ListingApiPlan(
            source_kind="embedded_html",
            api_url=source_url,
            html_selector=_selector_for_element(element, attr_name),
            html_attribute=attr_name,
            items_json_path=best_path,
            sample_items=best_items[:3],
            total_count=len(best_items),
        ),
        score=best_score,
    )


def detect_embedded_structured_source(html: str, *, source_url: str) -> ListingApiPlan | None:
    """Find a likely embedded JSON listing source in rendered HTML."""
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    best: _EmbeddedCandidate | None = None

    for element in soup.find_all(True):
        for attr_name, attr_value in element.attrs.items():
            if not isinstance(attr_name, str):
                continue
            if isinstance(attr_value, list):
                continue
            if not isinstance(attr_value, str) or len(attr_value) < 100:
                continue

            attr_name_l = attr_name.lower()
            attr_value_l = attr_value.lstrip()
            if (
                attr_name_l not in _ATTR_HINTS
                and not attr_name_l.startswith("data-")
                and "json" not in attr_name_l
                and not attr_value_l.startswith("{")
                and not attr_value_l.startswith("[")
            ):
                continue

            data = _parse_json_blob(attr_value)
            if data is None:
                continue

            candidate = _build_candidate(
                source_url=source_url,
                element=element,
                attr_name=attr_name,
                data=data,
                source_hint=attr_name_l,
            )
            if candidate and (best is None or candidate.score > best.score):
                best = candidate

    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        script_text = script.string or script.get_text()
        if not script_text or len(script_text) < 100:
            continue
        if script_type and "json" not in script_type and not script_text.lstrip().startswith(("{", "[")):
            continue

        data = _parse_json_blob(script_text)
        if data is None:
            continue

        candidate = _build_candidate(
            source_url=source_url,
            element=script,
            attr_name=None,
            data=data,
            source_hint=script_type or "script",
        )
        if candidate and (best is None or candidate.score > best.score):
            best = candidate

    return best.plan if best else None


def _get_raw_embedded_json(html: str, plan: ListingApiPlan) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    selector = (plan.html_selector or "").strip()
    if not selector:
        return None

    try:
        element = soup.select_one(selector)
    except Exception:
        return None
    if element is None:
        return None

    if plan.html_attribute:
        value = element.get(plan.html_attribute)
        return value if isinstance(value, str) else None

    return element.get_text()


def extract_structured_items_from_html(html: str, plan: ListingApiPlan) -> list[dict[str, str | None]]:
    """Materialize flattened items from an embedded structured source."""
    raw_json = _get_raw_embedded_json(html, plan)
    data = _parse_json_blob(raw_json)
    if data is None:
        return []

    obj: Any = data
    if plan.items_json_path:
        for key in plan.items_json_path.split("."):
            if isinstance(obj, dict) and key in obj:
                obj = obj[key]
            else:
                obj = None
                break

    if isinstance(obj, list):
        return [_flatten_item(item) for item in obj if isinstance(item, dict)]
    if isinstance(obj, dict):
        return [_flatten_item(obj)]
    return []
