"""
A deliberately simple robots.txt checker -- not a full RFC 9309
implementation. It fetches robots.txt once per domain, parses `Disallow`/
`Allow` lines for the matching user-agent group (falling back to `*`), and
answers "is this path allowed". Any failure to fetch or parse robots.txt
is treated as "allowed" (fail-open), since a missing/broken robots.txt is
common and should not block a crawl of otherwise-public pages.
"""
from __future__ import annotations

from urllib.parse import urlparse


class SimpleRobots:
    def __init__(self, fetch_text_fn, user_agent: str):
        """fetch_text_fn(url) -> str | None. Injected so callers can reuse
        their own HTTP session/mocking without this module depending on
        `requests` directly."""
        self._fetch_text_fn = fetch_text_fn
        self._user_agent = user_agent.lower()
        self._rules_by_domain: dict[str, list[tuple[str, str]]] = {}
        self._fetched_domains: set[str] = set()

    def _load(self, domain: str, scheme: str) -> None:
        if domain in self._fetched_domains:
            return
        self._fetched_domains.add(domain)
        robots_url = f"{scheme}://{domain}/robots.txt"
        try:
            text = self._fetch_text_fn(robots_url)
        except Exception:
            text = None
        self._rules_by_domain[domain] = self._parse(text or "")

    def _parse(self, text: str) -> list[tuple[str, str]]:
        # Standard robots.txt grouping: consecutive User-agent lines form
        # one group's agent set; the first Allow/Disallow line after them
        # ends that group's agent list (a later User-agent line starts a
        # new group). We keep every group whose agent set matches '*' or
        # our own user-agent, and flatten their rules together (later
        # matches win on longest-prefix, same as a real robots.txt).
        groups: list[dict] = []
        current_group: dict | None = None
        expecting_agents = True
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field = field.strip().lower()
            value = value.strip()
            if field == "user-agent":
                if current_group is None or not expecting_agents:
                    current_group = {"agents": [], "rules": []}
                    groups.append(current_group)
                current_group["agents"].append(value.lower())
                expecting_agents = True
            elif field in ("disallow", "allow"):
                if current_group is None:
                    continue
                expecting_agents = False
                current_group["rules"].append((field, value))

        rules: list[tuple[str, str]] = []
        matched_specific = False
        for group in groups:
            if self._user_agent in group["agents"]:
                matched_specific = True
        for group in groups:
            is_match = (
                self._user_agent in group["agents"]
                if matched_specific
                else "*" in group["agents"]
            )
            if is_match:
                rules.extend(group["rules"])
        return rules

    def is_allowed(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except ValueError:
            return True
        domain = parsed.netloc.lower()
        scheme = parsed.scheme or "https"
        self._load(domain, scheme)
        rules = self._rules_by_domain.get(domain, [])
        path = parsed.path or "/"

        best_match_len = -1
        best_match_allow = True
        for field, value in rules:
            if path.startswith(value):
                if len(value) > best_match_len:
                    best_match_len = len(value)
                    best_match_allow = field == "allow"
        return best_match_allow
