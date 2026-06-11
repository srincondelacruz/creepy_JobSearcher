"""Infojobs.net scraper.

Infojobs is a React SPA: the listing HTML is empty, but the full search state
(offers incl. complete descriptions) ships embedded as
`window.__INITIAL_PROPS__ = JSON.parse("...")`. We parse that JSON directly;
the old BeautifulSoup selectors remain as fallback.

Province IDs are Infojobs-internal (NOT postal codes): Madrid = 33.
"""
import json
import re
import urllib.parse
from typing import Optional

from loguru import logger

from .base_scraper import BaseScraper, JobListing, parse_salary_range


_PROVINCE_MAP = {
    "madrid": "33",  # verified live 2026-06
    "remoto": "",
    "teletrabajo": "",
}

_INITIAL_PROPS_RE = re.compile(
    r'window\.__INITIAL_PROPS__\s*=\s*JSON\.parse\(("(?:[^"\\]|\\.)*")\)'
)

_REMOTE_TERMS = {"teletrabajo", "remoto", "remote", "100% remoto", "trabajo desde casa"}


class InfojobsScraper(BaseScraper):
    SOURCE = "infojobs"
    BASE_URL = "https://www.infojobs.net"

    # ── Public API ────────────────────────────────────────────────────────────

    def search(self, keyword: str, location: str = "Madrid") -> list[JobListing]:
        url = self._build_search_url(keyword, location)
        logger.info(f"[Infojobs] Searching: {keyword!r} @ {location!r}")
        results: list[JobListing] = []
        page = 1
        while True:
            page_url = f"{url}&page={page}" if page > 1 else url
            resp = self._get(page_url)
            if not resp:
                break
            listings = self._parse_json_offers(resp.text)
            if listings is None:  # JSON blob missing — fall back to HTML selectors
                soup = self._parse_html(resp.text)
                listings = self._parse_listing_page(soup)
            if not listings:
                break
            results.extend(listings)
            # stop after 3 pages to be respectful
            if page >= 3:
                break
            page += 1
            self._sleep()
        logger.info(f"[Infojobs] Found {len(results)} listings")
        return results

    def get_details(self, listing: JobListing) -> JobListing:
        # JSON offers already carry the full description — nothing to fetch
        if listing.description or not listing.url:
            return listing
        self._sleep()
        resp = self._get(listing.url)
        if not resp:
            return listing
        soup = self._parse_html(resp.text)
        listing.description = self._extract_description(soup)
        if not listing.salary_raw:
            listing.salary_raw = self._extract_salary(soup)
        if listing.salary_raw and listing.salary_min is None:
            listing.salary_min, listing.salary_max = parse_salary_range(listing.salary_raw)
        return listing

    # ── Embedded-JSON parsing (primary path, 2026 SPA layout) ────────────────

    def _parse_json_offers(self, html: str) -> Optional[list[JobListing]]:
        """Extract offers from window.__INITIAL_PROPS__. None if blob absent."""
        m = _INITIAL_PROPS_RE.search(html)
        if not m:
            return None
        try:
            data = json.loads(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[Infojobs] __INITIAL_PROPS__ parse failed: {e}")
            return None

        listings: list[JobListing] = []
        for o in data.get("offers", []):
            link = o.get("link", "")
            if link.startswith("//"):
                link = "https:" + link
            teleworking = (o.get("teleworking") or "").lower()
            salary_raw = o.get("salaryDescription") or ""
            salary_min, salary_max = (
                parse_salary_range(salary_raw) if salary_raw else (None, None)
            )
            listings.append(JobListing(
                source=self.SOURCE,
                title=o.get("title", "").strip(),
                company=o.get("companyName", "").strip(),
                url=self._abs(link),
                location=o.get("city", ""),
                description=(o.get("description") or "")[:4000],
                salary_raw=salary_raw,
                salary_min=salary_min,
                salary_max=salary_max,
                remote="teletrabajo" in teleworking or "remoto" in teleworking,
            ))
        return [j for j in listings if j.title]

    # ── URL builders ──────────────────────────────────────────────────────────

    def _build_search_url(self, keyword: str, location: str) -> str:
        params: dict[str, str] = {
            "keyword": keyword,
            "sinceDate": "_7_DAYS",
            "sortBy": "PUBLICATION_DATE",
        }
        loc_lower = location.lower()
        province_id = _PROVINCE_MAP.get(loc_lower)
        if province_id:
            params["provinceIds"] = province_id
        elif loc_lower in {"remoto", "teletrabajo"}:
            params["telecommuting"] = "1"
        else:
            # default to Madrid
            params["provinceIds"] = "33"

        qs = urllib.parse.urlencode(params)
        return f"{self.BASE_URL}/jobsearch/search-results/list.xhtml?{qs}"

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_listing_page(self, soup) -> list[JobListing]:
        listings: list[JobListing] = []

        # Infojobs uses data-* attributes heavily; try several selector strategies
        cards = (
            soup.select("li[data-id]")          # primary selector
            or soup.select(".ij-OfferList-item") # legacy
            or soup.select("article.offer-item") # fallback
        )

        if not cards:
            # last-resort: look for any anchor pointing to /ofertas-trabajo/
            anchors = soup.find_all("a", href=re.compile(r"/oferta-trabajo/"))
            for a in anchors:
                title = a.get_text(strip=True)
                url = self._abs(a.get("href", ""))
                if title and url:
                    listings.append(JobListing(source=self.SOURCE, title=title, url=url))
            return listings

        for card in cards:
            try:
                listings.append(self._parse_card(card))
            except Exception as e:
                logger.debug(f"[Infojobs] card parse error: {e}")
        return [j for j in listings if j.title]

    def _parse_card(self, card) -> JobListing:
        # Title + URL
        title_el = (
            card.select_one("h2 a")
            or card.select_one(".ij-OfferList-item-title a")
            or card.select_one("a[data-test='offer-title']")
            or card.select_one("a.js-o-link")
        )
        title = title_el.get_text(strip=True) if title_el else ""
        url = self._abs(title_el.get("href", "")) if title_el else ""

        # Company
        company_el = (
            card.select_one(".ij-OfferList-item-company")
            or card.select_one("[data-test='company-name']")
            or card.select_one(".company")
        )
        company = company_el.get_text(strip=True) if company_el else ""

        # Location
        location_el = (
            card.select_one(".ij-OfferList-item-location")
            or card.select_one("[data-test='location']")
            or card.select_one(".location")
        )
        location = location_el.get_text(strip=True) if location_el else ""

        # Salary
        salary_el = (
            card.select_one(".ij-OfferList-item-salary")
            or card.select_one("[data-test='salary']")
            or card.select_one(".salary")
        )
        salary_raw = salary_el.get_text(strip=True) if salary_el else ""
        salary_min, salary_max = parse_salary_range(salary_raw) if salary_raw else (None, None)

        remote = any(t in location.lower() for t in _REMOTE_TERMS)

        return JobListing(
            source=self.SOURCE,
            title=title,
            company=company,
            url=url,
            location=location,
            salary_raw=salary_raw,
            salary_min=salary_min,
            salary_max=salary_max,
            remote=remote,
        )

    def _extract_description(self, soup) -> str:
        desc_el = (
            soup.select_one(".job-description")
            or soup.select_one("#jobDescriptionText")
            or soup.select_one("[data-test='job-description']")
            or soup.select_one(".ij-OfferDetail-description")
        )
        if desc_el:
            return desc_el.get_text(separator="\n", strip=True)[:4000]
        return ""

    def _extract_salary(self, soup) -> str:
        sal_el = (
            soup.select_one("[data-test='salary']")
            or soup.select_one(".ij-OfferDetail-salary")
        )
        return sal_el.get_text(strip=True) if sal_el else ""

    def _has_next_page(self, soup) -> bool:
        return bool(
            soup.select_one("a[rel='next']")
            or soup.select_one(".pagination-next:not(.disabled)")
        )

    def _abs(self, href: str) -> str:
        if not href:
            return ""
        if href.startswith("http"):
            return href
        return self.BASE_URL + href
