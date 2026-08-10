"""Small, independent Epic catalog client.

This module intentionally has no imports from the Steam pipeline. The default
catalog provider exposes Epic catalog records through a JSON search endpoint;
the base URL is configurable so the collector can be switched later without
changing the database or discovery logic.
"""
from __future__ import annotations

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


class EpicClientError(RuntimeError):
    """Raised when the Epic catalog cannot be read reliably."""


def _value_by_key(items, key: str) -> str:
    for item in items or []:
        if isinstance(item, dict) and item.get("key") == key:
            return str(item.get("value") or "").strip()
    return ""


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

    slug = str(raw.get("productSlug") or raw.get("urlSlug") or "").strip()
    mappings = raw.get("offerMappings") or []
    if not slug:
        for mapping in mappings:
            if isinstance(mapping, dict) and mapping.get("pageSlug"):
                slug = str(mapping["pageSlug"]).strip()
                break

    store_url = str(raw.get("url") or "").strip()
    if not store_url and slug:
        store_url = f"https://store.epicgames.com/en-US/p/{urllib.parse.quote(slug)}"

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
    ):
        self.base_url = (base_url or os.environ.get("EPIC_CATALOG_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.country = country
        self.locale = locale
        self.timeout = timeout

    def _post(self, page: int, limit: int) -> dict:
        query = urllib.parse.urlencode({"country": self.country, "locale": self.locale})
        url = f"{self.base_url}/search?{query}"
        payload = {
            "offerType": "BASE_GAME",
            "sortBy": "upcoming",
            "sortDir": "asc",
            "page": page,
            "limit": limit,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "StudioReachEngine-Epic/1.0",
            },
            method="POST",
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
                    raise EpicClientError("Epic catalog response has no elements list")
                return data
            except (urllib.error.HTTPError, urllib.error.URLError,
                    TimeoutError, json.JSONDecodeError, EpicClientError) as exc:
                retryable = (
                    isinstance(exc, EpicClientError)
                    or not isinstance(exc, urllib.error.HTTPError)
                    or exc.code in (429, 500, 502, 503, 504)
                )
                if not retryable or attempt == 3:
                    raise EpicClientError(f"Epic catalog request failed: {exc}") from exc
                time.sleep(2 ** attempt)
        raise EpicClientError("Epic catalog request exhausted retries")

    def fetch_offer(self, offer_id: str) -> dict:
        """Fetch one Epic offer's full detail record."""
        if not offer_id:
            raise ValueError("offer_id is required")
        url = f"{self.base_url}/offers/{urllib.parse.quote(str(offer_id), safe='')}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "StudioReachEngine-Epic/1.0",
            },
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if not isinstance(data, dict) or data.get("id") != str(offer_id):
                    raise EpicClientError("Epic detail response does not match the requested offer")
                return data
            except (urllib.error.HTTPError, urllib.error.URLError,
                    TimeoutError, json.JSONDecodeError, EpicClientError) as exc:
                retryable = (
                    isinstance(exc, EpicClientError)
                    or not isinstance(exc, urllib.error.HTTPError)
                    or exc.code in (429, 500, 502, 503, 504)
                )
                if not retryable or attempt == 3:
                    raise EpicClientError(f"Epic detail request failed for {offer_id}: {exc}") from exc
                time.sleep(2 ** attempt)
        raise EpicClientError("Epic detail request exhausted retries")

    def fetch_upcoming(self, page_size: int = DEFAULT_PAGE_SIZE) -> list[dict]:
        """Fetch and deduplicate the current upcoming base-game catalog."""
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")

        products = {}
        page = 1
        expected = None
        while page <= MAX_PAGES:
            data = self._post(page, page_size)
            expected = data.get("total", expected)
            for raw in data["elements"]:
                product = normalize_product(raw)
                products.setdefault(product["epic_key"], product)

            if not data["elements"] or len(products) >= (expected or 0):
                break
            if len(data["elements"]) < page_size:
                break
            page += 1
        else:
            raise EpicClientError("Epic catalog exceeded the pagination safety limit")

        if not products:
            raise EpicClientError("Epic catalog returned no upcoming base games")
        return list(products.values())
