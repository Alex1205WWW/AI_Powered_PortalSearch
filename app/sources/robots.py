"""robots.txt + Content-Signal handling, cached per source and re-checked weekly. Fails closed:
a robots.txt we cannot read (403, 5xx, network error) means "do not crawl"; 404 means no restrictions."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Source
from app.sources.http import HttpClient, get_client

log = logging.getLogger(__name__)


@dataclass
class RobotsInfo:
    status_code: int | None
    text: str = ""
    content_signal: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    def allows(self, url: str, user_agent: str | None = None) -> bool:
        if self.status_code == 404:
            return True
        if self.status_code != 200:
            return False
        parser = RobotFileParser()
        parser.parse(self.text.splitlines())
        return parser.can_fetch(user_agent or _ua_token(), url)


def _ua_token() -> str:
    return settings.http_user_agent.split("/")[0].strip() or "*"


def parse_content_signal(text: str) -> dict[str, str]:
    """`Content-Signal: search=yes, ai-input=no, ai-train=no` -> {"search": "yes", "ai-input": "no", ...}"""
    signals: dict[str, str] = {}
    for line in text.splitlines():
        if line.lower().startswith("content-signal:"):
            for part in line.split(":", 1)[1].split(","):
                if "=" in part:
                    key, value = part.split("=", 1)
                    signals[key.strip().lower()] = value.strip().lower()
    return signals


def fetch(base_url: str, client: HttpClient | None = None) -> RobotsInfo:
    client = client or get_client()
    url = urljoin(base_url if base_url.endswith("/") else base_url + "/", "robots.txt")
    try:
        response = client.get(url, retries=1)
    except Exception as exc:  # network errors fail closed
        return RobotsInfo(status_code=None, error=str(exc))
    if response.status_code == 200:
        return RobotsInfo(status_code=200, text=response.text, content_signal=parse_content_signal(response.text))
    return RobotsInfo(status_code=response.status_code)


def refresh_source(session: Session, source: Source, client: HttpClient | None = None) -> RobotsInfo:
    if not source.base_url.startswith("http"):
        return RobotsInfo(status_code=None, error="not an http source")
    info = fetch(source.base_url, client)
    source.robots_fetched_at = datetime.now(timezone.utc)
    source.robots_status = info.status_code
    source.robots_txt = info.text or None
    source.robots_allowed = info.allows(source.base_url)
    source.content_signal = ", ".join(f"{k}={v}" for k, v in info.content_signal.items()) or None
    source.signal_search = info.content_signal.get("search")
    source.signal_ai_input = info.content_signal.get("ai-input")
    source.signal_ai_train = info.content_signal.get("ai-train")
    if info.status_code not in (200, 404):
        source.status = "blocked"
        source.last_error = info.error or f"robots.txt HTTP {info.status_code}"
    elif source.status == "blocked":
        source.status = "ok"
        source.last_error = None
    log.info("robots refreshed", extra={"source": source.key})
    return info


STALE_AFTER = timedelta(days=7)
RECHECK_CAP = 60  # per run: 60 fetches at 2 s is ~2 min, and the queue drains over a few nights


def refresh_all(session: Session, *, cap: int = RECHECK_CAP, stale_after: timedelta = STALE_AFTER,
                force: bool = False) -> list[tuple[str, int | None, bool | None]]:
    """Re-check the robots.txt of the sources whose copy is oldest, bounded.

    Day 4 had 8 sources and could refresh them all every week. With the agency sites that becomes hundreds,
    so the sweep now takes the staleest first up to `cap` and leaves the rest for the next run; a source is
    skipped entirely while its cached copy is still fresh. `force=True` keeps the old behaviour for the CLI,
    where the operator is explicitly asking for a full refresh.
    """
    cutoff = datetime.now(timezone.utc) - stale_after
    stmt = select(Source).order_by(Source.robots_fetched_at.asc().nullsfirst(), Source.id)
    if not force:
        stmt = stmt.where(or_(Source.robots_fetched_at.is_(None), Source.robots_fetched_at < cutoff)).limit(cap)
    results = []
    for source in session.scalars(stmt):
        info = refresh_source(session, source)
        results.append((source.key, info.status_code, source.robots_allowed))
    return results


def is_allowed(source: Source, url: str) -> bool:
    """Cheap check from the cached robots.txt; call before every fetch on a source."""
    if source.robots_status is None:
        return False
    info = RobotsInfo(status_code=source.robots_status, text=source.robots_txt or "")
    return info.allows(url)


def same_host(url: str, base_url: str) -> bool:
    return urlparse(url).netloc.lower().removeprefix("www.") == urlparse(base_url).netloc.lower().removeprefix("www.")
