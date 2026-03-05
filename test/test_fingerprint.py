"""Tests for app.utils.fingerprint — platform fingerprinting."""

from __future__ import annotations

import pytest

from app.utils.fingerprint import PlatformInfo, fingerprint


class TestFingerprint:
    def test_wordpress_detection(self):
        html = '<html><head><link rel="stylesheet" href="/wp-content/themes/test/style.css"></head><body></body></html>'
        info = fingerprint(html)
        assert info.cms == "WordPress"

    def test_shopify_detection(self):
        html = '<html><head><script src="https://cdn.shopify.com/s/files/1/theme.js"></script></head><body></body></html>'
        info = fingerprint(html)
        assert info.ecommerce == "Shopify"

    def test_nextjs_detection(self):
        html = '<html><body><div id="__next"></div><script src="/_next/static/chunks/main.js"></script></body></html>'
        info = fingerprint(html)
        assert info.js_framework == "Next.js"
        assert info.is_spa is True

    def test_angular_detection(self):
        html = '<html ng-app="myApp"><body ng-version="15.0.0"></body></html>'
        info = fingerprint(html)
        assert info.js_framework == "Angular"
        assert info.is_spa is True

    def test_react_detection(self):
        html = '<html><body><div id="root" data-reactroot></div></body></html>'
        info = fingerprint(html)
        assert info.js_framework == "React"
        assert info.is_spa is True

    def test_vue_detection(self):
        html = '<html><body><div id="vue-app" v-cloak></div></body></html>'
        info = fingerprint(html)
        assert info.js_framework == "Vue.js"

    def test_woocommerce_detection(self):
        html = '<html><body><div class="woocommerce-page"></div></body></html>'
        info = fingerprint(html)
        assert info.ecommerce == "WooCommerce"

    def test_magento_detection(self):
        html = '<html><head><script>require(["mage/cookies"])</script></head><body></body></html>'
        info = fingerprint(html)
        assert info.ecommerce == "Magento"

    def test_cloudflare_cdn_from_headers(self):
        info = fingerprint("<html><body></body></html>", headers={"cf-ray": "abc123", "server": "cloudflare"})
        assert info.cdn == "Cloudflare"
        assert info.server == "cloudflare"

    def test_server_header(self):
        info = fingerprint("<html><body></body></html>", headers={"server": "nginx/1.25.3"})
        assert info.server == "nginx"

    def test_api_evidence_from_html(self):
        html = '<html><body><script>fetch("/api/v2/items")</script></body></html>'
        info = fingerprint(html)
        assert info.has_api is True

    def test_api_evidence_from_content_type(self):
        info = fingerprint("<html></html>", headers={"content-type": "application/json"})
        assert info.has_api is True

    def test_meta_generator_wordpress(self):
        html = '<html><head><meta name="generator" content="WordPress 6.4.2"></head><body></body></html>'
        info = fingerprint(html)
        assert info.cms == "WordPress"
        assert any("Generator" in s for s in info.signals)

    def test_unknown_platform(self):
        info = fingerprint("<html><body><p>Plain page</p></body></html>")
        assert info.summary() == "Unknown"

    def test_wix_detection(self):
        html = '<html><body><script src="https://static.wixstatic.com/js/main.js"></script></body></html>'
        info = fingerprint(html)
        assert info.cms == "Wix"

    def test_squarespace_detection(self):
        html = '<html><body><script src="https://static.squarespace.com/assets/main.js"></script></body></html>'
        info = fingerprint(html)
        assert info.cms == "Squarespace"


class TestPlatformInfo:
    def test_summary(self):
        info = PlatformInfo(cms="WordPress", js_framework="React", is_spa=True)
        s = info.summary()
        assert "WordPress" in s
        assert "React" in s
        assert "SPA" in s

    def test_to_dict(self):
        info = PlatformInfo(cms="Shopify", ecommerce="Shopify", has_api=True)
        d = info.to_dict()
        assert d["cms"] == "Shopify"
        assert d["ecommerce"] == "Shopify"
        assert d["has_api"] is True
