"""One HTTP client for every source: per-host politeness delay, retry with backoff, honest User-Agent."""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

import httpx

from app.config import settings

log = logging.getLogger(__name__)
RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(self, user_agent: str, timeout_s: float, default_delay_s: float) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.5"},
            timeout=timeout_s,
            follow_redirects=True,
        )
        self._default_delay = default_delay_s
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()

    def _wait_for_host(self, host: str, delay_s: float) -> None:
        with self._lock:
            now = time.monotonic()
            due = self._last_request.get(host, 0.0) + delay_s
            sleep_for = max(0.0, due - now)
            self._last_request[host] = max(now, due)
        if sleep_for > 0:
            time.sleep(sleep_for)

    def get(self, url: str, *, delay_s: float | None = None, retries: int = 3, headers: dict | None = None) -> httpx.Response:
        host = urlparse(url).netloc
        delay = self._default_delay if delay_s is None else delay_s
        for attempt in range(retries + 1):
            self._wait_for_host(host, delay)
            try:
                response = self._client.get(url, headers=headers)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise
                log.warning("transport error on %s (%s), retry %d", url, exc, attempt + 1)
                time.sleep(2.0 * (attempt + 1))
                continue
            if response.status_code in RETRY_STATUSES and attempt < retries:
                log.warning("http %s on %s, retry %d", response.status_code, url, attempt + 1)
                time.sleep(5.0 * (attempt + 1))
                continue
            return response
        raise RuntimeError("unreachable")

    def get_bytes(self, url: str, **kwargs) -> bytes:
        response = self.get(url, **kwargs)
        response.raise_for_status()
        return response.content

    def close(self) -> None:
        self._client.close()


_client: HttpClient | None = None


def get_client() -> HttpClient:
    global _client
    if _client is None:
        _client = HttpClient(settings.http_user_agent, settings.http_timeout_s, settings.http_default_delay_s)
    return _client
