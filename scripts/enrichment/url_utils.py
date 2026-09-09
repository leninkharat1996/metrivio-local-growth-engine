"""URL validation/normalization helpers for Step 9 website enrichment."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAM_EXACT = {
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src",
}

DEFAULT_ALLOWED_SCHEMES = ("http", "https")


def is_valid_url(url: str, allowed_schemes=DEFAULT_ALLOWED_SCHEMES) -> bool:
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in allowed_schemes:
        return False
    if not parsed.netloc:
        return False
    return True


def _is_tracking_param(key: str) -> bool:
    key_lower = key.lower()
    if key_lower in TRACKING_PARAM_EXACT:
        return True
    return any(key_lower.startswith(prefix) for prefix in TRACKING_PARAM_PREFIXES)


def normalize_url(url: str) -> str:
    """Strip the fragment and tracking query params, drop a trailing
    slash on the path (except root "/"), lowercase scheme/host. Query
    params that survive are sorted for determinism."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    kept_params = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    kept_params.sort()
    query = urlencode(kept_params)

    return urlunparse((scheme, netloc, path, parsed.params, query, ""))


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def same_domain(url_a: str, url_b: str) -> bool:
    def _host(u: str) -> str:
        host = urlparse(u).netloc.lower()
        return host[4:] if host.startswith("www.") else host

    return _host(url_a) == _host(url_b)


def resolve_link(base_url: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith("#"):
        return None
    if href.lower().startswith(("mailto:", "tel:", "javascript:")):
        return None
    try:
        resolved = urljoin(base_url, href)
    except ValueError:
        return None
    if not is_valid_url(resolved):
        return None
    return normalize_url(resolved)
