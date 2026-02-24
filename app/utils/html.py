"""HTML utilities for trimming pages before sending to the LLM."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment


def simplify_html(raw_html: str, *, max_chars: int = 80_000) -> str:
    """Strip scripts, styles, comments, and collapse whitespace.

    Keeps the structural HTML that the LLM needs for planning, while
    reducing token usage dramatically.
    """
    soup = BeautifulSoup(raw_html, "lxml")

    # Remove elements that add noise
    for tag in soup.find_all(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    # Remove HTML comments
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # Remove common boilerplate sections
    for selector in ["header", "footer", "nav", ".cookie-banner", "#cookie-consent"]:
        for el in soup.select(selector):
            el.decompose()

    html_str = str(soup)

    # Collapse whitespace
    html_str = re.sub(r"\n\s*\n", "\n", html_str)
    html_str = re.sub(r"  +", " ", html_str)

    if len(html_str) > max_chars:
        html_str = html_str[:max_chars] + "\n<!-- TRUNCATED -->"
    return html_str


def extract_text(element_html: str) -> str:
    """Extract visible text from an HTML fragment."""
    soup = BeautifulSoup(element_html, "lxml")
    return soup.get_text(separator=" ", strip=True)
