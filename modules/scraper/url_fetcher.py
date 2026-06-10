"""On-demand URL content extractor.

Strategy:
  1. Try Playwright (headless Chromium) — handles JS-rendered offers (LinkedIn,
     Infojobs SPA views, company ATS pages).
  2. Fall back to requests + BeautifulSoup if Playwright isn't installed or fails.

Returns clean text plus best-effort metadata (title, company, salary, location).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup
from loguru import logger

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Tags whose text is noise, not offer content
_STRIP_TAGS = ["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]


@dataclass
class FetchResult:
    url: str
    ok: bool
    text: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    salary: str = ""
    method: str = ""              # "playwright" | "requests"
    error: str = ""
    meta: dict = field(default_factory=dict)

    def to_meta(self) -> dict:
        return {
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "salary": self.salary,
            "source_url": self.url,
            "fetch_method": self.method,
        }


class URLFetcher:
    def __init__(self, settings: dict):
        cfg = settings.get("fetcher", {})
        self.prefer_playwright = cfg.get("prefer_playwright", True)
        self.timeout = cfg.get("timeout", 30)
        self.wait_until = cfg.get("wait_until", "networkidle")
        self.max_chars = cfg.get("max_content_chars", 12000)
        self.block_resources = set(cfg.get("block_resources", ["image", "media", "font"]))

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, url: str) -> FetchResult:
        if not _valid_url(url):
            return FetchResult(url=url, ok=False, error="Invalid URL")

        if self.prefer_playwright:
            result = self._fetch_playwright(url)
            if result.ok:
                return result
            logger.warning(f"Playwright fetch failed ({result.error}); falling back to requests")

        result = self._fetch_requests(url)
        if result.ok:
            return result

        # If we skipped playwright initially, try it as a last resort
        if not self.prefer_playwright:
            pw = self._fetch_playwright(url)
            if pw.ok:
                return pw
        return result

    # ── Playwright ────────────────────────────────────────────────────────────

    def _fetch_playwright(self, url: str) -> FetchResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return FetchResult(url=url, ok=False, error="playwright not installed")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=_UA, locale="es-ES")
                page = context.new_page()

                # Block heavy assets for speed
                if self.block_resources:
                    page.route(
                        "**/*",
                        lambda route: (
                            route.abort()
                            if route.request.resource_type in self.block_resources
                            else route.continue_()
                        ),
                    )

                page.goto(url, wait_until=self.wait_until, timeout=self.timeout * 1000)
                html = page.content()
                browser.close()
            return self._parse_html(url, html, method="playwright")
        except Exception as e:
            return FetchResult(url=url, ok=False, error=f"playwright: {e}")

    # ── Requests fallback ─────────────────────────────────────────────────────

    def _fetch_requests(self, url: str) -> FetchResult:
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _UA, "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return self._parse_html(url, resp.text, method="requests")
        except requests.RequestException as e:
            return FetchResult(url=url, ok=False, error=f"requests: {e}")

    # ── HTML → text + metadata ────────────────────────────────────────────────

    def _parse_html(self, url: str, html: str, method: str) -> FetchResult:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(_STRIP_TAGS):
            tag.decompose()

        title = self._extract_title(soup)
        company = self._extract_meta_like(soup, ["company", "empresa", "hiringOrganization"])
        location = self._extract_meta_like(soup, ["location", "ubicacion", "jobLocation"])
        salary = self._extract_salary(soup)

        # Prefer a main/article container; else whole body
        main = (
            soup.find("main")
            or soup.find("article")
            or soup.find(id=re.compile(r"(job|offer|oferta|description|descripcion)", re.I))
            or soup.body
            or soup
        )
        text = main.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)[: self.max_chars]

        if len(text) < 100:
            return FetchResult(
                url=url, ok=False, method=method,
                error=f"extracted text too short ({len(text)} chars) — likely blocked or JS-only",
            )

        return FetchResult(
            url=url, ok=True, text=text, title=title, company=company,
            location=location, salary=salary, method=method,
        )

    @staticmethod
    def _extract_title(soup) -> str:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            return og["content"].strip()
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        if soup.title:
            return soup.title.get_text(strip=True)
        return ""

    @staticmethod
    def _extract_meta_like(soup, keys: list[str]) -> str:
        for key in keys:
            el = soup.find(attrs={"itemprop": key}) or soup.find(attrs={"data-test": re.compile(key, re.I)})
            if el:
                txt = el.get("content") or el.get_text(strip=True)
                if txt:
                    return txt.strip()[:120]
        return ""

    @staticmethod
    def _extract_salary(soup) -> str:
        for el in soup.find_all(string=re.compile(r"€|\beur\b|salario|salary", re.I)):
            txt = el.strip()
            if "€" in txt and len(txt) < 80:
                return txt
        return ""


def _valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://", url.strip(), re.I))


def load_fetcher(settings: dict) -> URLFetcher:
    return URLFetcher(settings)
