"""Tests for HTML utility functions."""

from __future__ import annotations

import pytest

from app.utils.html import extract_text, simplify_html


class TestSimplifyHtml:
    """Test simplify_html helper."""

    FULL_PAGE = """
    <html>
    <head><title>Test</title><style>body{color:red}</style></head>
    <body>
        <script>alert('xss')</script>
        <noscript>Please enable JS</noscript>
        <header><nav>Menu</nav></header>
        <div class="cookie-banner">We use cookies</div>
        <main>
            <div class="exhibitor-list">
                <div class="item">
                    <h3>Acme Corp</h3>
                    <p>Widget maker</p>
                </div>
                <div class="item">
                    <h3>Beta Inc</h3>
                    <p>Tool maker</p>
                </div>
            </div>
        </main>
        <footer>Copyright 2025</footer>
        <!-- This is a comment -->
    </body>
    </html>
    """

    def test_removes_script_tags(self):
        result = simplify_html(self.FULL_PAGE)
        assert "alert(" not in result
        assert "<script" not in result

    def test_removes_style_tags(self):
        result = simplify_html(self.FULL_PAGE)
        assert "color:red" not in result
        assert "<style" not in result

    def test_removes_noscript(self):
        result = simplify_html(self.FULL_PAGE)
        assert "Please enable JS" not in result

    def test_removes_html_comments(self):
        result = simplify_html(self.FULL_PAGE)
        assert "This is a comment" not in result

    def test_removes_header_footer_nav(self):
        result = simplify_html(self.FULL_PAGE)
        assert "Menu" not in result
        assert "Copyright 2025" not in result

    def test_removes_cookie_banner(self):
        result = simplify_html(self.FULL_PAGE)
        assert "We use cookies" not in result

    def test_preserves_content(self):
        result = simplify_html(self.FULL_PAGE)
        assert "Acme Corp" in result
        assert "Widget maker" in result
        assert "Beta Inc" in result

    def test_collapses_whitespace(self):
        html = "<html><body><p>  lots   of   space  </p>\n\n\n\n<p>two</p></body></html>"
        result = simplify_html(html)
        assert "\n\n" not in result

    def test_truncation(self):
        html = "<html><body>" + "x" * 100_000 + "</body></html>"
        result = simplify_html(html, max_chars=500)
        assert len(result) <= 500 + len("\n<!-- TRUNCATED -->")
        assert "<!-- TRUNCATED -->" in result

    def test_custom_max_chars(self):
        html = "<html><body>" + "<p>test</p>" * 1000 + "</body></html>"
        result = simplify_html(html, max_chars=200)
        assert len(result) <= 200 + len("\n<!-- TRUNCATED -->")


class TestSimplifyHtmlAggressive:
    """Test aggressive mode of simplify_html."""

    HTML_WITH_IMAGES = """
    <html>
    <head><title>Test</title><link rel="stylesheet" href="/style.css"></head>
    <body>
        <img src="https://example.com/photo.jpg" alt="Logo" class="logo">
        <video src="video.mp4"></video>
        <audio src="audio.mp3"></audio>
        <div style="background: url(tracking.gif)">Content</div>
        <a href="javascript:void(0)">Click</a>
        <div data-tracking="1234" onclick="track()">
            <h3>Exhibitor</h3>
        </div>
        <div class="social-share">Share this</div>
        <div class="ad-container">Ad here</div>
        <div id="cookie-popup">Accept cookies?</div>
    </body>
    </html>
    """

    def test_strips_head(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert "<head" not in result

    def test_replaces_img_src(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert "photo.jpg" not in result
        assert "[removed]" in result

    def test_preserves_img_alt(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert "Logo" in result

    def test_removes_video_audio(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert "<video" not in result
        assert "<audio" not in result

    def test_removes_inline_styles(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert 'style="' not in result
        assert "tracking.gif" not in result

    def test_cleans_javascript_hrefs(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert "javascript:" not in result

    def test_removes_data_attributes(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert "data-tracking" not in result

    def test_removes_event_handlers(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert "onclick" not in result

    def test_removes_social_share(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert "Share this" not in result

    def test_removes_ad_containers(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert "Ad here" not in result

    def test_removes_cookie_popups(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert "Accept cookies?" not in result

    def test_preserves_exhibitor_content(self):
        result = simplify_html(self.HTML_WITH_IMAGES, aggressive=True)
        assert "Exhibitor" in result


class TestExtractText:
    """Test extract_text helper."""

    def test_simple_paragraph(self):
        assert extract_text("<p>Hello World</p>") == "Hello World"

    def test_nested_html(self):
        html = "<div><h3>Name</h3><span>City</span></div>"
        result = extract_text(html)
        assert "Name" in result
        assert "City" in result

    def test_strips_tags(self):
        html = "<b>Bold</b> and <i>italic</i>"
        result = extract_text(html)
        assert "<b>" not in result
        assert "Bold" in result
        assert "italic" in result

    def test_empty_html(self):
        assert extract_text("") == ""

    def test_whitespace_handling(self):
        html = "<p>  lots   of   space  </p>"
        result = extract_text(html)
        # get_text(strip=True) strips leading/trailing whitespace per element
        # but does not collapse internal whitespace
        assert "lots" in result
        assert "space" in result
        assert not result.startswith(" ")
        assert not result.endswith(" ")
