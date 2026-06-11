"""Tecnoempleo.com scraper.

Tecnoempleo renders HTML server-side and is more scraping-friendly than Infojobs.
Search URL: https://www.tecnoempleo.com/busqueda-empleo.php?te={keyword}&pr=28&tf=1

Province IDs: Madrid = 28.
tf=1 means published in last 7 days.
"""
import re
import urllib.parse

from loguru import logger

from .base_scraper import BaseScraper, JobListing, parse_salary_range


_REMOTE_TERMS = {"teletrabajo", "remoto", "remote", "teletraball"}


class TecnoempleoScraper(BaseScraper):
    SOURCE = "tecnoempleo"
    BASE_URL = "https://www.tecnoempleo.com"

    # ── Public API ────────────────────────────────────────────────────────────

    def search(self, keyword: str, location: str = "Madrid") -> list[JobListing]:
        url = self._build_search_url(keyword, location)
        logger.info(f"[Tecnoempleo] Searching: {keyword!r} @ {location!r}")
        results: list[JobListing] = []
        page = 1
        while True:
            page_url = f"{url}&pagina={page}" if page > 1 else url
            resp = self._get(page_url)
            if not resp:
                break
            soup = self._parse_html(resp.text)
            listings = self._parse_listing_page(soup)
            if not listings:
                break
            results.extend(listings)
            if page >= 3 or not self._has_next_page(soup):
                break
            page += 1
            self._sleep()
        logger.info(f"[Tecnoempleo] Found {len(results)} listings")
        return results

    def get_details(self, listing: JobListing) -> JobListing:
        if not listing.url:
            return listing
        self._sleep()
        resp = self._get(listing.url)
        if not resp:
            return listing
        soup = self._parse_html(resp.text)
        listing.description = self._extract_description(soup)
        if not listing.salary_raw:
            listing.salary_raw = self._extract_salary_detail(soup)
        if listing.salary_raw and listing.salary_min is None:
            listing.salary_min, listing.salary_max = parse_salary_range(listing.salary_raw)
        return listing

    # ── URL builder ───────────────────────────────────────────────────────────

    def _build_search_url(self, keyword: str, location: str) -> str:
        params: dict[str, str] = {
            "te": keyword,
            "tf": "1",  # last 7 days
        }
        loc_lower = location.lower()
        if loc_lower not in {"remoto", "teletrabajo", "remote"}:
            params["pr"] = "28"  # Madrid province
        else:
            params["teletrabajo"] = "1"
        qs = urllib.parse.urlencode(params)
        return f"{self.BASE_URL}/busqueda-empleo.php?{qs}"

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_listing_page(self, soup) -> list[JobListing]:
        listings: list[JobListing] = []

        # Primary selector: offer cards (2026 layout: clickable bordered divs)
        cards = (
            soup.select("div.p-3.border.rounded")
            or soup.select("article.p-3")
            or soup.select(".oferta-empleo")
            or soup.select("div.row.py-3.border-bottom")
        )

        if not cards:
            # Fallback: scan for offer links (offer URLs end in /rf-<hex>)
            for a in soup.find_all("a", href=re.compile(r"/rf-[0-9a-f]+")):
                title = a.get_text(strip=True)
                url = self._abs(a.get("href", ""))
                if title and url:
                    listings.append(JobListing(source=self.SOURCE, title=title, url=url))
            return listings

        for card in cards:
            try:
                listings.append(self._parse_card(card))
            except Exception as e:
                logger.debug(f"[Tecnoempleo] card parse error: {e}")
        return [j for j in listings if j.title]

    def _parse_card(self, card) -> JobListing:
        # Title + URL
        title_el = (
            card.select_one("h3 a")
            or card.select_one("h2 a")
            or card.select_one("a.font-weight-bold")
            or card.select_one("a[href*='oferta-trabajo']")
        )
        title = title_el.get_text(strip=True) if title_el else ""
        url = self._abs(title_el.get("href", "")) if title_el else ""

        # Company (2026 layout: muted link right under the title)
        company_el = (
            card.select_one("a.text-primary.link-muted")
            or card.select_one(".empresa")
            or card.select_one("span.company")
        )
        company = company_el.get_text(strip=True) if company_el else ""

        # Location — <b> holds it clean ("100% remoto", "Madrid"); spans are noisy
        location = ""
        for el in card.select("b") + card.select("span"):
            text = el.get_text(strip=True)
            low = text.lower()
            if any(t in low for t in ("madrid", "remoto", "teletrabajo", "híbrido", "hibrido")):
                location = re.split(r"\s*-\s*|\d{2}/\d{2}/\d{4}", text)[0].strip()
                break

        # Salary — extract "39.000€ - 45.000€" style ranges from card text
        m = re.search(
            r"\d{2,3}(?:\.\d{3})?\s*€(?:\s*-\s*\d{2,3}(?:\.\d{3})?\s*€)?(?:\s*b/a)?",
            card.get_text(" ", strip=True),
        )
        salary_raw = m.group(0) if m else ""
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
            soup.select_one("div.fs--16.text-gray-800")  # 2026 layout
            or soup.select_one("#descripcion-oferta")
            or soup.select_one(".descripcion-oferta")
            or soup.select_one("div.job-description")
            or soup.select_one("section.descripcion")
        )
        if desc_el:
            return desc_el.get_text(separator="\n", strip=True)[:4000]
        # Heuristic fallback: shortest container with offer-like long text
        best = ""
        for d in soup.find_all(["div", "section"]):
            t = d.get_text(separator="\n", strip=True)
            low = t.lower()
            if 500 < len(t) < 8000 and ("funciones" in low or "requisitos" in low or "experiencia" in low):
                if not best or len(t) < len(best):
                    best = t
        return best[:4000]

    def _extract_salary_detail(self, soup) -> str:
        for el in soup.select("li, span, div"):
            text = el.get_text(strip=True)
            if "€" in text and len(text) < 60:
                return text
        return ""

    def _has_next_page(self, soup) -> bool:
        return bool(
            soup.select_one("a[rel='next']")
            or soup.select_one(".pagination .next:not(.disabled)")
            or soup.find("a", string=re.compile(r"Siguiente|›|»"))
        )

    def _abs(self, href: str) -> str:
        if not href:
            return ""
        if href.startswith("http"):
            return href
        return self.BASE_URL + href.lstrip("/")
