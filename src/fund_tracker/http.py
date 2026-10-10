"""HTTP client with browser impersonation (curl_cffi when available) and retries."""
from __future__ import annotations

import logging
import random
import time
from typing import Any

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class HttpError(RuntimeError):
    pass


class Http:
    """Thin wrapper that prefers curl_cffi (TLS fingerprint of a real Chrome) and
    falls back to requests. Both expose .get() returning an object with
    status_code / text / json()."""

    def __init__(self, timeout: int = 40, retries: int = 3, impersonate: bool = True):
        self.timeout = timeout
        self.retries = retries
        self.backend = "requests"
        self._session: Any = None
        if impersonate:
            try:
                from curl_cffi import requests as curl_requests  # type: ignore

                self._session = curl_requests.Session(impersonate="chrome")
                self.backend = "curl_cffi"
            except Exception as exc:  # pragma: no cover - depends on environment
                log.info("curl_cffi not available (%s); falling back to requests", exc)
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.headers.update({"User-Agent": BROWSER_UA})

    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            allow_statuses: tuple[int, ...] = (200,)) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self._session.get(url, params=params, headers=headers, timeout=self.timeout)
                if resp.status_code in allow_statuses:
                    return resp
                last_exc = HttpError(f"HTTP {resp.status_code} for {url}")
                if resp.status_code in (400, 401, 403, 404):
                    # retrying will not help for client errors
                    raise last_exc
            except HttpError:
                raise
            except Exception as exc:  # network errors
                last_exc = exc
            sleep = min(2 ** attempt, 10) + random.random()
            log.debug("retry %d for %s in %.1fs (%s)", attempt, url, sleep, last_exc)
            time.sleep(sleep)
        raise HttpError(str(last_exc))
