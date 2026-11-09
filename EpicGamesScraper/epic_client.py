"""Small, independent Epic catalog client.

This module intentionally has no imports from the Steam pipeline. The default
catalog provider (egdata, https://docs.egdata.app) exposes Epic catalog records
through ``GET /search/v2/search`` and ``GET /offers/{offer_id}``; the base URL is
configurable so the collector can be switched later without changing the
database or discovery logic.

egdata scores paginated catalog traversal and may answer a suspicious client
with ``SESSION_CHALLENGE_REQUIRED`` (HTTP 200). Its documented recovery is an
on-demand SHA-256 proof-of-work handshake at ``POST /auth/challenge`` followed
by one retry with the ``x-egdata-session`` header, which this client implements.
"""
from __future__ import annotations

import email.utils
import hashlib
import itertools
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_BASE_URL = "https://api.egdata.app"
DEFAULT_COUNTRY = "US"
DEFAULT_LOCALE = "en-US"
DEFAULT_PAGE_SIZE = 100
MAX_PAGES = 1000
SEARCH_PATH = "/search/v2/search"
CHALLENGE_PATH = "/auth/challenge"
USER_AGENT = "StudioReachEngine-Epic/1.0"
SESSION_HEADER = "x-egdata-session"
CHALLENGE_CODE = "SESSION_CHALLENGE_REQUIRED"
MAX_POW_DIFFICULTY = 28
MAX_RETRY_AFTER = 120
RETRYABLE_STATUS = (204, 429, 500, 502, 503, 504)
# Minimum spacing between catalog requests; egdata asks bulk consumers to be gentle.
DEFAULT_REQUEST_INTERVAL = 1.0
# egdata scores behaviour over a 60-second window; a tarpitted client waits it out.
TARPIT_BACKOFF = 65


class EpicClientError(RuntimeError):
    """Raised when the Epic catalog cannot be read reliably."""


def _value_by_key(items, key: str) -> str:
    """Read a custom attribute from either the list or the keyed-object form."""
    if isinstance(items, dict):
        item = items.get(key)
        value = item.get("value") if isinstance(item, dict) else item
        return str(value or "").strip()
    for item in items or []:
        if isinstance(item, dict) and item.get("key") == key:
            return str(item.get("value") or "").strip()
    return ""


def _leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte:
            return bits + 8 - byte.bit_length()
        bits += 8
    return bits


def solve_pow(challenge: str, difficulty: int) -> str:
    """Find a nonce so SHA-256(challenge + ':' + nonce) has `difficulty` leading zero bits."""
    if type(difficulty) is not int or not 0 <= difficulty <= MAX_POW_DIFFICULTY:
        raise EpicClientError(f"Epic catalog challenge has unsupported difficulty {difficulty!r}")
    prefix = f"{challenge}:".encode("utf-8")
    for counter in itertools.count():
        nonce = format(counter, "x")
        if _leading_zero_bits(hashlib.sha256(prefix + nonce.encode("ascii")).digest()) >= difficulty:
            return nonce
    raise AssertionError("unreachable")


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed is None:
            return None
        seconds = parsed.timestamp() - time.time()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def _names(items) -> list[str]:
    result = []
    for item in items or []:
        value = item.get("name") if isinstance(item, dict) else item
        if value and str(value).strip():
            result.append(str(value).strip())
    return result


def _categories(items) -> list[str]:
    result = []
    for item in items or []:
        value = item.get("path") if isinstance(item, dict) else item
        if value and str(value).strip():
            result.append(str(value).strip())
    return result


def normalize_product(raw: dict) -> dict:
    """Convert one provider record into the Epic database contract."""
    if not isinstance(raw, dict):
        raise EpicClientError("Epic catalog returned a non-object product")

    namespace = str(raw.get("namespace") or "").strip()
    offer_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not offer_id or not title:
        raise EpicClientError("Epic catalog product is missing id or title")

    custom = raw.get("customAttributes") or []
    developer = str(raw.get("developerDisplayName") or "").strip()
    publisher = str(raw.get("publisherDisplayName") or "").strip()
    developer = developer or _value_by_key(custom, "developerName")
    publisher = publisher or _value_by_key(custom, "publisherName")

    mappings = raw.get("offerMappings") or []
    home_slug = next(
        (str(mapping.get("pageSlug")).strip() for mapping in mappings
         if isinstance(mapping, dict)
         and mapping.get("pageType") == "productHome"
         and mapping.get("pageSlug")),
        "",
    )
    slug = str(home_slug or raw.get("urlSlug") or raw.get("productSlug") or "").strip()
    if not slug:
        for mapping in mappings:
            if isinstance(mapping, dict) and mapping.get("pageSlug"):
                slug = str(mapping["pageSlug"]).strip()
                break

    store_url = str(raw.get("url") or "").strip()
    if not store_url and slug:
        store_url = f"https://store.epicgames.com/en-US/p/{urllib.parse.quote(slug, safe='')}"

    # Namespace represents the Epic product. Multiple base-game offers can share
    # one namespace, so it is the stable game-level key when available.
    epic_key = f"namespace:{namespace}" if namespace else f"offer:{offer_id}"
    return {
        "epic_key": epic_key,
        "namespace": namespace,
        "offer_id": offer_id,
        "title": title,
        "short_description": str(raw.get("description") or "").strip(),
        "developers": developer,
        "publishers": publisher,
        "genres": _categories(raw.get("categories")),
        "tags": _names(raw.get("tags")),
        "release_date": raw.get("releaseDate"),
        "pc_release_date": raw.get("pcReleaseDate"),
        "store_url": store_url,
        "raw": raw,
    }


class EpicCatalogClient:
    """Read-only client for the Epic catalog search provider."""

    def __init__(
        self,
        base_url: str | None = None,
        country: str = DEFAULT_COUNTRY,
        locale: str = DEFAULT_LOCALE,
        timeout: int = 45,
        request_interval: float = DEFAULT_REQUEST_INTERVAL,
    ):
        self.base_url = (base_url or os.environ.get("EPIC_CATALOG_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.country = country
        self.locale = locale
        self.timeout = timeout
        self.request_interval = request_interval
        self.session_token: str | None = None
        self._last_request = 0.0

    def _open(self, request: urllib.request.Request):
        wait = self._last_request + self.request_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        finally:
            self._last_request = time.monotonic()
        with response:
            return response.status, response.read()

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.session_token:
            headers[SESSION_HEADER] = self.session_token
        headers.update(extra or {})
        return headers

    def _post_challenge(self, body: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{CHALLENGE_PATH}",
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers({"Content-Type": "application/json"}),
            method="POST",
        )
        _, raw = self._open(request)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise EpicClientError("Epic catalog challenge returned a non-object")
        return data

    def _establish_session(self) -> None:
        """Run egdata's documented proof-of-work handshake and store the session token."""
        self.session_token = None
        data = self._post_challenge({})
        for _ in range(3):
            if isinstance(data.get("token"), str) and data["token"]:
                self.session_token = data["token"]
                return
            challenge = data.get("challenge")
            if not isinstance(challenge, str) or not challenge:
                raise EpicClientError(f"Epic catalog challenge response is malformed: {data.get('error') or data}")
            if str(data.get("algorithm") or "SHA-256").upper() != "SHA-256":
                raise EpicClientError(f"Epic catalog challenge uses unsupported algorithm {data.get('algorithm')!r}")
            nonce = solve_pow(challenge, data.get("difficulty"))
            # An invalid or expired proof returns a fresh challenge; solve that one.
            data = self._post_challenge({"challenge": challenge, "nonce": nonce})
        raise EpicClientError("Epic catalog challenge could not be completed")

    def _get_json(self, path: str, params: dict | None, validate, label: str) -> dict:
        """GET a JSON object, handling challenges, tarpits, rate limits and retries."""
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        challenged = False
        attempt = 0
        while True:
            delay = None
            try:
                status, raw = self._open(urllib.request.Request(url, headers=self._headers()))
                if status == 204:
                    delay = TARPIT_BACKOFF
                    raise EpicClientError("provider throttled the request (HTTP 204 tarpit)")
                data = json.loads(raw.decode("utf-8"))
                error = data.get("error") if isinstance(data, dict) else None
                if isinstance(error, dict) and error.get("code") == CHALLENGE_CODE:
                    if challenged:
                        raise EpicClientError("provider challenged the request again after a new session")
                    challenged = True
                    self._establish_session()
                    continue
                validate(data)
                return data
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE_STATUS or attempt == 3:
                    raise EpicClientError(f"{label}: {exc}") from exc
                if exc.code == 429:
                    delay = _retry_after(exc)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                    UnicodeDecodeError, EpicClientError) as exc:
                if attempt == 3:
                    raise EpicClientError(f"{label}: {exc}") from exc
            time.sleep(delay if delay is not None else 2 ** attempt)
            attempt += 1
            challenged = False

    def _search_page(self, page: int, limit: int) -> dict:
        params = {
            "country": self.country,
            "locale": self.locale,
            "offerType": "BASE_GAME",
            "sortBy": "upcoming",
            "facets": "none",
            "page": page,
            "limit": limit,
        }

        def validate(data):
            if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
                raise EpicClientError("Epic catalog response has no offers list")

        return self._get_json(SEARCH_PATH, params, validate, "Epic catalog request failed")

    def fetch_offer(self, offer_id: str) -> dict:
        """Fetch one Epic offer's full detail record."""
        if not offer_id:
            raise ValueError("offer_id is required")

        def validate(data):
            if not isinstance(data, dict) or data.get("id") != str(offer_id):
                raise EpicClientError("Epic detail response does not match the requested offer")

        path = f"/offers/{urllib.parse.quote(str(offer_id), safe='')}"
        return self._get_json(path, {"country": self.country, "locale": self.locale}, validate,
                              f"Epic detail request failed for {offer_id}")

    def fetch_upcoming(self, page_size: int = DEFAULT_PAGE_SIZE) -> list[dict]:
        """Fetch and deduplicate the current upcoming base-game catalog."""
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")

        products = {}
        offers = set()
        page = 1
        expected = None
        while page <= MAX_PAGES:
            data = self._search_page(page, page_size)
            total = data.get("total")
            if total is not None:
                if type(total) is not int or total < 0:
                    raise EpicClientError("Epic catalog returned an invalid total")
                if expected is not None and total != expected:
                    raise EpicClientError("Epic catalog total changed during pagination")
                expected = total
            for raw in data["offers"]:
                product = normalize_product(raw)
                offers.add((product["namespace"], product["offer_id"]))
                products.setdefault(product["epic_key"], product)

            response_limit = data.get("limit") or page_size
            try:
                response_limit = int(response_limit)
            except (TypeError, ValueError):
                response_limit = page_size
            if response_limit < 1:
                raise EpicClientError("Epic catalog returned an invalid page limit")

            if not data["offers"] or (expected is not None and len(offers) >= expected):
                break
            if len(data["offers"]) < response_limit:
                break
            page += 1
        else:
            raise EpicClientError("Epic catalog exceeded the pagination safety limit")

        if expected is not None and len(offers) != expected:
            raise EpicClientError(
                f"Epic catalog total mismatch: expected {expected}, fetched {len(offers)} offers")
        if not products:
            raise EpicClientError("Epic catalog returned no upcoming base games")
        return list(products.values())
