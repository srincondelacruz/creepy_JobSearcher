"""Joinrs.com scraper (Playwright + auth).

Joinrs is a Next.js SPA gated behind login. After auth it serves AI-curated
job recommendations based on the user's profile — there is no keyword search.
All recommended offers are returned on the first search() call; subsequent
keyword calls return an empty list (dedup prevents DB duplicates anyway).

Credentials via env: JOINRS_EMAIL, JOINRS_PASSWORD
Session persisted at data/joinrs_session.json — avoids re-login on each run.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from .base_scraper import BaseScraper, JobListing, parse_salary_range

_SESSION_FILE = Path("data/joinrs_session.json")
_BASE_URL = "https://www.joinrs.com"
_OFFERS_CANDIDATES = ["/es/offers", "/es/feed", "/es/home", "/es/dashboard"]
_REMOTE_TERMS = {"teletrabajo", "remoto", "remote", "100% remoto"}


class JoinrsScraper(BaseScraper):
    SOURCE = "joinrs"
    BASE_URL = _BASE_URL

    def __init__(self, config: dict):
        super().__init__(config)
        self._email = os.getenv("JOINRS_EMAIL", "")
        self._password = os.getenv("JOINRS_PASSWORD", "")
        self._fetched = False  # only scrape once per run

    def search(self, keyword: str, location: str = "Madrid") -> list[JobListing]:
        if self._fetched:
            return []

        if not self._email or not self._password:
            logger.error("[Joinrs] JOINRS_EMAIL / JOINRS_PASSWORD not set — skipping")
            return []

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("[Joinrs] playwright missing — run: pip install playwright && playwright install chromium")
            return []

        listings: list[JobListing] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = self._build_context(browser)
            page = ctx.new_page()

            try:
                if not self._ensure_logged_in(page):
                    return []

                offers_url = self._find_offers_page(page)
                if not offers_url:
                    logger.error("[Joinrs] Could not locate offers page after login")
                    return []

                listings = self._parse_listings(page, offers_url)
                self._save_session(ctx)
                self._fetched = True
            except Exception as e:
                logger.error(f"[Joinrs] Scrape error: {e}")
            finally:
                browser.close()

        logger.info(f"[Joinrs] Found {len(listings)} listings")
        return listings

    def get_details(self, listing: JobListing) -> JobListing:
        return listing

    # ── Session management ────────────────────────────────────────────────────

    def _build_context(self, browser):
        if _SESSION_FILE.exists():
            try:
                state = json.loads(_SESSION_FILE.read_text())
                return browser.new_context(storage_state=state)
            except Exception as e:
                logger.debug(f"[Joinrs] Stale session, re-login: {e}")
        return browser.new_context()

    def _save_session(self, ctx) -> None:
        try:
            _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            _SESSION_FILE.write_text(json.dumps(ctx.storage_state()))
            logger.debug("[Joinrs] Session saved")
        except Exception as e:
            logger.debug(f"[Joinrs] Could not save session: {e}")

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _ensure_logged_in(self, page) -> bool:
        page.goto(f"{_BASE_URL}/es", wait_until="domcontentloaded", timeout=30_000)
        self._sleep()

        body = page.inner_text("body")
        if self._is_logged_in(body):
            logger.info("[Joinrs] Session valid — skipping login")
            return True

        logger.info("[Joinrs] Logging in...")
        try:
            # Step 1: open email form
            page.click("text=Continúa con tu email", timeout=8_000)
            time.sleep(1.5)

            # Step 2: fill email → Continuar
            page.fill("input[type=email]", self._email)
            page.click("text=Continuar", timeout=5_000)
            time.sleep(1.5)

            # Step 3: fill password → Continuar
            page.fill("input[type=password]", self._password)
            page.click("text=Continuar", timeout=5_000)
            time.sleep(4)

            body = page.inner_text("body")
            if not self._is_logged_in(body):
                logger.error("[Joinrs] Login failed — check JOINRS_EMAIL / JOINRS_PASSWORD")
                return False

            logger.info("[Joinrs] Login successful")
            return True
        except Exception as e:
            logger.error(f"[Joinrs] Login error: {e}")
            return False

    def _is_logged_in(self, body_text: str) -> bool:
        signals = ["cerrar sesión", "mensajes", "notificaciones", "experiencias"]
        body_lower = body_text.lower()
        return any(s in body_lower for s in signals)

    # ── Navigation ────────────────────────────────────────────────────────────

    def _find_offers_page(self, page) -> Optional[str]:
        # Try clicking the Ofertas nav button first (most reliable post-login)
        try:
            page.click("text=Ofertas", timeout=5_000)
            time.sleep(3)
            url = page.url
            body = page.inner_text("body")
            if self._looks_like_offers_page(body):
                logger.info(f"[Joinrs] Offers page via nav button: {url}")
                return url
        except Exception:
            pass

        # Fallback: try candidate URLs
        for path in _OFFERS_CANDIDATES:
            try:
                page.goto(f"{_BASE_URL}{path}", wait_until="domcontentloaded", timeout=20_000)
                time.sleep(3)
                body = page.inner_text("body")
                if self._looks_like_offers_page(body):
                    logger.info(f"[Joinrs] Offers page found: {_BASE_URL}{path}")
                    return page.url
            except Exception:
                continue

        return None

    def _looks_like_offers_page(self, body_text: str) -> bool:
        signals = ["empresa", "solicitar", "aplicar", "ver oferta", "ver más", "ubicación", "jornada"]
        body_lower = body_text.lower()
        return sum(1 for s in signals if s in body_lower) >= 2

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_listings(self, page, url: str) -> list[JobListing]:
        listings: list[JobListing] = []

        # Try up to 3 pages (some sites paginate)
        for page_num in range(1, 4):
            cards = self._extract_cards(page)
            if not cards:
                logger.warning(f"[Joinrs] No job cards found on page {page_num} — selectors may need updating")
                # Log page HTML excerpt for debugging
                try:
                    content = page.content()
                    logger.debug(f"[Joinrs] Page HTML excerpt: {content[:2000]}")
                except Exception:
                    pass
                break

            for card_data in cards:
                listing = self._card_to_listing(card_data)
                if listing:
                    listings.append(listing)

            # Check for next page button
            if not self._click_next_page(page):
                break
            time.sleep(3)

        return listings

    def _extract_cards(self, page) -> list[dict]:
        """Try multiple selector strategies; return list of card data dicts."""
        # Strategy 1: data-testid patterns
        selectors = [
            "[data-testid*='offer']",
            "[data-testid*='job']",
            "[class*='OfferCard']",
            "[class*='JobCard']",
            "[class*='offer-card']",
            "[class*='job-card']",
            "article",
        ]

        for selector in selectors:
            try:
                elements = page.query_selector_all(selector)
                if elements and len(elements) >= 2:
                    logger.debug(f"[Joinrs] Card selector '{selector}' found {len(elements)} elements")
                    return [self._extract_card_data(el) for el in elements]
            except Exception:
                continue

        # Strategy 2: look for repeating li/div with job-like content
        try:
            # Find elements that contain company-like text patterns
            candidates = page.evaluate("""() => {
                const results = [];
                // Look for elements that likely are job cards
                const allDivs = Array.from(document.querySelectorAll('div, li, article'));
                const jobCards = allDivs.filter(el => {
                    const text = el.innerText || '';
                    const children = el.children.length;
                    // Heuristic: has 3-10 children and contains location/company signals
                    return children >= 2 && children <= 15 &&
                           text.length > 50 && text.length < 2000 &&
                           (text.includes('Madrid') || text.includes('Remoto') ||
                            text.includes('Barcelona') || text.includes('España'));
                });
                // Deduplicate by taking parent-most cards
                return jobCards.slice(0, 30).map(el => ({
                    html: el.innerHTML.substring(0, 500),
                    text: el.innerText.substring(0, 400),
                    tag: el.tagName,
                    classes: el.className.substring(0, 100)
                }));
            }""")
            if candidates and len(candidates) >= 2:
                logger.debug(f"[Joinrs] Heuristic found {len(candidates)} card candidates")
                return [{"text": c["text"], "html": c["html"]} for c in candidates]
        except Exception as e:
            logger.debug(f"[Joinrs] Heuristic card extraction error: {e}")

        return []

    def _extract_card_data(self, element) -> dict:
        data: dict = {}
        try:
            data["text"] = element.inner_text()
            # Title: first heading
            for sel in ["h2", "h3", "h4", "[class*='title']", "strong"]:
                el = element.query_selector(sel)
                if el:
                    data["title"] = el.inner_text(timeout=2000).strip()
                    break
            # Company
            for sel in ["[class*='company']", "[class*='employer']", "small", "p"]:
                el = element.query_selector(sel)
                if el:
                    text = el.inner_text(timeout=2000).strip()
                    if text and "title" not in data.get("title", "").lower():
                        data["company"] = text
                        break
            # URL
            link = element.query_selector("a")
            if link:
                data["url"] = link.get_attribute("href") or ""
        except Exception:
            pass
        return data

    def _card_to_listing(self, card_data: dict) -> Optional[JobListing]:
        text = card_data.get("text", "")
        if not text or len(text) < 10:
            return None

        # If we have structured data use it, else parse raw text
        title = card_data.get("title", "")
        company = card_data.get("company", "")
        url = card_data.get("url", "")

        if not title:
            # Parse first non-empty line as title
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if lines:
                title = lines[0]
            if len(lines) > 1:
                company = lines[1]

        if not title:
            return None

        if url and not url.startswith("http"):
            url = _BASE_URL + url

        remote = any(t in text.lower() for t in _REMOTE_TERMS)
        location = ""
        for line in text.splitlines():
            line = line.strip()
            if any(loc in line for loc in ["Madrid", "Barcelona", "Valencia", "Sevilla", "España", "Remoto", "Teletrabajo"]):
                location = line
                break

        salary_raw = ""
        for line in text.splitlines():
            if "€" in line or "EUR" in line or "salary" in line.lower() or "salario" in line.lower():
                salary_raw = line.strip()
                break
        salary_min, salary_max = parse_salary_range(salary_raw) if salary_raw else (None, None)

        return JobListing(
            source=self.SOURCE,
            title=title[:200],
            company=company[:200],
            url=url,
            location=location,
            description=text[:4000],
            salary_raw=salary_raw,
            salary_min=salary_min,
            salary_max=salary_max,
            remote=remote,
        )

    def _click_next_page(self, page) -> bool:
        for selector in ["a[rel='next']", "button:has-text('Siguiente')", "[aria-label='Next']", "[class*='next']"]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.click()
                    return True
            except Exception:
                continue
        return False
