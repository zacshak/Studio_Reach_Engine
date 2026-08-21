"""Minimal client for QuickEmailVerification's single and bulk REST APIs."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API_BASE = "https://api.quickemailverification.com/v1"


class QEVError(RuntimeError):
    """QuickEmailVerification request or response failure."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def is_safe_to_send(result: dict) -> bool:
    """Handle the boolean and string booleans used in QEV's examples."""
    value = result.get("safe_to_send", False)
    return value is True or (isinstance(value, str) and value.lower() == "true")


class QuickEmailVerification:
    def __init__(self, api_key: str, *, timeout: float = 30, opener=None):
        if not api_key.strip():
            raise ValueError("api_key is required")
        self.api_key = api_key.strip()
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls, **kwargs) -> "QuickEmailVerification":
        api_key = os.environ.get("QEV_API_KEY", "")
        if not api_key:
            raise QEVError("QEV_API_KEY is not set")
        return cls(api_key, **kwargs)

    def verify(self, email: str, *, sandbox: bool = False) -> dict:
        if not email.strip():
            raise ValueError("email is required")
        endpoint = "verify/sandbox" if sandbox else "verify"
        query = urllib.parse.urlencode({"email": email.strip(), "apikey": self.api_key})
        return self._json(urllib.request.Request(f"{API_BASE}/{endpoint}?{query}"))

    def submit_bulk(self, csv_path: str | Path) -> dict:
        path = Path(csv_path)
        if not path.is_file():
            raise ValueError(f"CSV file does not exist: {path}")
        if path.suffix.lower() != ".csv":
            raise ValueError("bulk verification requires a .csv file")

        boundary = uuid.uuid4().hex
        filename = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        # ponytail: in-memory upload; stream only if lists outgrow available RAM.
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="upload"; filename="{filename}"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            f"{API_BASE}/bulk-verify",
            data=body,
            method="POST",
            headers={
                "Authorization": f"token {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-QEV-Filename": filename,
            },
        )
        return self._json(request)

    def bulk_status(self, job_id: str) -> dict:
        if not job_id.strip():
            raise ValueError("job_id is required")
        job = urllib.parse.quote(job_id.strip(), safe="")
        query = urllib.parse.urlencode({"apikey": self.api_key})
        return self._json(
            urllib.request.Request(f"{API_BASE}/bulk-verify/status/{job}?{query}")
        )

    def wait_for_bulk(
        self, job_id: str, *, poll_interval: float = 10, timeout: float = 3600
    ) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            result = self.bulk_status(job_id)
            status = str(result.get("status", "")).lower()
            if status == "completed":
                return result
            if status == "failed":
                raise QEVError(result.get("message") or "bulk verification failed")
            if time.monotonic() >= deadline:
                raise QEVError(f"bulk verification timed out after {timeout:g} seconds")
            time.sleep(poll_interval)

    def download_report(self, url: str, output_path: str | Path) -> Path:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "api.quickemailverification.com":
            raise QEVError("refusing report URL outside QuickEmailVerification")
        output = Path(output_path)
        try:
            with self._open(urllib.request.Request(url), timeout=self.timeout) as response:
                data = response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            raise QEVError(f"report download failed: {exc.reason if hasattr(exc, 'reason') else exc}") from exc
        output.write_bytes(data)
        return output

    def _json(self, request: urllib.request.Request) -> dict:
        try:
            with self._open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = self._error_message(exc.read())
            raise QEVError(
                f"QuickEmailVerification HTTP {exc.code}: {detail}",
                status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            detail = exc.reason if hasattr(exc, "reason") else exc
            raise QEVError(f"QuickEmailVerification request failed: {detail}") from exc

        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise QEVError("QuickEmailVerification returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise QEVError("QuickEmailVerification returned an invalid response")
        if result.get("success") is False or str(result.get("success", "")).lower() == "false":
            raise QEVError(result.get("message") or "QuickEmailVerification request failed")
        return result

    @staticmethod
    def _error_message(raw: bytes) -> str:
        try:
            result = json.loads(raw)
            return str(result.get("message") or "request failed")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            return "request failed"
