"""
Thin adapter around the Scrapling framework (https://github.com/D4Vinci/Scrapling).

Scrapling is the primary fetching + HTML parsing library for the Step 9
website enrichment layer. Everything the crawler needs from it goes through
this one module so that:

- tests can substitute a fake fetch implementation (no real network calls),
- there is exactly one place where Scrapling's API surface is used, and
- it is auditable that we only use Scrapling's PLAIN HTTP fetcher and its
  parser.

Scrapling APIs used (verified against the installed scrapling 0.4.15):
    scrapling.Fetcher.get(url, timeout=..., headers=..., follow_redirects=...)
        -> scrapling.engines.toolbelt.custom.Response
           (a Selector subclass with .status/.reason/.headers/.url)
    scrapling.Selector(content=<raw html str>, url=<str>)
        -> offline parser; used directly for local HTML fixtures in tests
    Selector.css(...) / .attrib / .text / .get_all_text(ignore_tags=...)

EXPLICITLY NOT USED (and never to be added here): StealthyFetcher,
DynamicFetcher, `impersonate`/`stealthy_headers` browser-fingerprint
options, proxies/proxy rotation, retries-as-bypass, CAPTCHA solving, or any
other mechanism for defeating an access control. If a site blocks us, the
crawler records the failure and stops crawling that domain.
"""
from __future__ import annotations


def get_selector_class():
    """Return Scrapling's parser class (imported lazily)."""
    from scrapling import Selector

    return Selector


def parse_html(html, url=None):
    """Parse a raw HTML string with Scrapling's own parser -- no network.

    This is the same parser object Scrapling hands back from a real fetch,
    so tests that feed local fixture HTML through here exercise the real
    Scrapling parsing path.
    """
    selector_class = get_selector_class()
    return selector_class(content=html or "", url=url or "")


class ScraplingFetcher:
    """Plain-HTTP fetch adapter backed by ``scrapling.Fetcher``.

    One instance per enrichment run. `get()` returns Scrapling's Response
    (which is also its parsed Selector) or raises -- callers classify the
    outcome. No stealth/anti-bot options are passed, on purpose.
    """

    def __init__(self, timeout=10, user_agent=None):
        self.timeout = timeout
        self.user_agent = user_agent
        self._fetcher = None

    @property
    def fetcher(self):
        if self._fetcher is None:
            from scrapling import Fetcher

            self._fetcher = Fetcher
        return self._fetcher

    def headers(self):
        return {"User-Agent": self.user_agent} if self.user_agent else {}

    def get(self, url):
        """Perform one plain HTTP GET through Scrapling's Fetcher."""
        return self.fetcher.get(
            url,
            timeout=self.timeout,
            headers=self.headers(),
            follow_redirects=True,
            # Identify honestly: no browser impersonation / stealth headers.
            stealthy_headers=False,
        )
