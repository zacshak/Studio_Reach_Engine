"""Minimal client for Abstract API's Email Reputation endpoint (second verification fallback).

Docs: https://docs.abstractapi.com/api/email-reputation. The free plan allows 100 requests
a month at 1 request per second; every request costs a credit, even a malformed one.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://emailreputation.abstractapi.com/v1/"
MIN_INTERVAL = 1.1  # seconds between calls; the free plan allows 1 request per second

# Undeliverable details that prove the address cannot receive mail. "full_mailbox" and
# "unavailable_server" can be temporary, so those stay unknown.
DEFINITIVE_UNDELIVERABLE = {"invalid_mailbox", "invalid_format", "dns_record_not_found"}


class AbstractError(RuntimeError):
    """Abstract API request or response failure (401 bad key, 422/403 quota, 429 rate)."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def map_reputation(email: str, data: dict) -> dict:
    """Map an Email Reputation response onto our verification dict, conservatively."""
    deliverability = data.get("email_deliverability") or {}
    quality = data.get("email_quality") or {}
    status = str(deliverability.get("status") or "unknown").lower()
    detail = str(deliverability.get("status_detail") or "").lower()
    catch_all = quality.get("is_catchall") is True
    disposable = quality.get("is_disposable") is True
    reason = f"{status}:{detail}" if detail else status

    if deliverability.get("is_format_valid") is False:
        result, reason = "invalid", "undeliverable:invalid_format"
    elif status == "undeliverable" and detail in DEFINITIVE_UNDELIVERABLE and not (
        catch_all and detail == "invalid_mailbox"
    ):
        result = "invalid"
    elif status == "undeliverable":
        result = "unknown"  # temporary or contradicted by a catch-all flag
    elif catch_all:
        # Same rule as our SMTP check: a catch-all domain cannot be proven either way.
        result, reason = "valid", f"{reason} (catch-all)"
    elif status == "deliverable":
        result = "valid"
    else:
        result = "unknown"

    return {
        "email": email,
        "result": result,
        "reason": reason,
        "accept_all": catch_all,
        "disposable": disposable,
        "safe_to_send": result == "valid" and (catch_all or not disposable),
        "source": "abstract",
    }


class AbstractEmailReputation:
    def __init__(self, api_key: str, *, timeout: float = 30, opener=None,
                 sleep=time.sleep, clock=time.monotonic):
        if not api_key.strip():
            raise ValueError("api_key is required")
        self.api_key = api_key.strip()
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._sleep, self._clock = sleep, clock
        self._last_call: float | None = None

    @classmethod
    def from_env(cls, **kwargs) -> "AbstractEmailReputation":
        api_key = os.environ.get("ABSTRACT_API_KEY", "")
        if not api_key.strip():
            raise AbstractError("ABSTRACT_API_KEY is not set")
        return cls(api_key, **kwargs)

    def verify(self, email: str) -> dict:
        email = email.strip()
        if not email:
            raise ValueError("email is required")
        query = urllib.parse.urlencode({"api_key": self.api_key, "email": email})
        request = urllib.request.Request(f"{API_BASE}?{query}")
        try:
            data = self._json(request)
        except AbstractError as exc:
            if exc.status_code != 429:
                raise
            data = self._json(request)  # one retry after the per-second limit
        return map_reputation(email, data)

    def _throttle(self) -> None:
        if self._last_call is not None:
            wait = MIN_INTERVAL - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _json(self, request: urllib.request.Request) -> dict:
        self._throttle()
        try:
            with self._open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise AbstractError(
                f"Abstract API HTTP {exc.code}: {self._error_message(exc.read())}",
                status_code=exc.code,
            ) from exc
        except (OSError, http.client.IncompleteRead) as exc:
            detail = exc.reason if hasattr(exc, "reason") else exc
            raise AbstractError(f"Abstract API request failed: {detail}") from exc

        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AbstractError("Abstract API returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise AbstractError("Abstract API returned an invalid response")
        if "error" in result:
            raise AbstractError(f"Abstract API error: {self._error_message(raw)}")
        if not isinstance(result.get("email_deliverability"), dict):
            raise AbstractError("Abstract API response has no email_deliverability")
        return result

    @staticmethod
    def _error_message(raw: bytes) -> str:
        """Error bodies look like {"error": {"message": ..., "code": ..., "details": ...}}."""
        try:
            error = json.loads(raw).get("error") or {}
            return str(error.get("message") or error.get("code") or "request failed")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            return "request failed"
