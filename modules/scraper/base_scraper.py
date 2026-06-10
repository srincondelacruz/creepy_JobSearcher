"""Abstract base scraper with shared session, rate-limiting, and retry logic."""
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup
from loguru import logger


@dataclass
class JobListing:
    source: str
    title: str
    company: str = ""
    url: str = ""
    location: str = ""
    salary_raw: str = ""
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    remote: bool = False
    description: str = ""
    posted_date: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "title": self.title,
            "company": self.company,
            "url": self.url,
            "location": self.location,
            "salary_raw": self.salary_raw,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "remote": self.remote,
            "description": self.description,
            "posted_date": self.posted_date,
        }


def parse_salary_range(text: str) -> tuple[Optional[float], Optional[float]]:
    """Best-effort extraction of salary numbers from a raw string like '28.000 - 35.000 €'."""
    import re
    nums = re.findall(r"[\d][.\d]*", text.replace(",", "."))
    clean = []
    for n in nums:
        try:
            val = float(n.replace(".", "").replace(",", "."))
            if 10_000 <= val <= 200_000:
                clean.append(val)
        except ValueError:
            continue
    if len(clean) >= 2:
        return min(clean[:2]), max(clean[:2])
    if len(clean) == 1:
        return clean[0], None
    return None, None


class BaseScraper(ABC):
    SOURCE = "base"

    def __init__(self, config: dict):
        self.config = config
        self.delay_min = config.get("delay_min", 2.0)
        self.delay_max = config.get("delay_max", 5.0)
        self.timeout = config.get("timeout", 30)
        self.max_retries = config.get("max_retries", 3)
        self._user_agents: list[str] = config.get("user_agents", [
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ])
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": random.choice(self._user_agents),
            "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        return s

    def _rotate_agent(self) -> None:
        self._session.headers["User-Agent"] = random.choice(self._user_agents)

    def _sleep(self) -> None:
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def _get(self, url: str, **kwargs) -> Optional[requests.Response]:
        self._rotate_agent()
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._session.get(url, timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    wait = 30 * attempt
                    logger.warning(f"Rate limited on {url}. Waiting {wait}s (attempt {attempt})")
                    time.sleep(wait)
                else:
                    logger.warning(f"HTTP {e.response.status_code if e.response else '?'} on {url} (attempt {attempt})")
                    if attempt == self.max_retries:
                        return None
            except requests.RequestException as e:
                logger.warning(f"Request error on {url}: {e} (attempt {attempt})")
                if attempt == self.max_retries:
                    return None
                time.sleep(2 ** attempt)
        return None

    def _parse_html(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    @abstractmethod
    def search(self, keyword: str, location: str = "Madrid") -> list[JobListing]:
        """Return list of job listings for a keyword+location combo."""
        ...

    @abstractmethod
    def get_details(self, listing: JobListing) -> JobListing:
        """Fetch full description for a listing (modifies in-place, returns it)."""
        ...
