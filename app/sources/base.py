"""Contract every source adapter implements. The worker only talks to adapters through this protocol."""
from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from app.db.models import CrawlRun, Criteria, Source


class SourceAdapter(Protocol):
    key: str

    def __init__(self, source: Source) -> None: ...

    def sweep(self, session: Session, run: CrawlRun) -> None:
        """Incremental pass: new or changed listings since the last watermark. Upserts into `listings`."""

    def initial_run(self, session: Session, run: CrawlRun, criteria: Criteria) -> None:
        """Targeted first pass for new criteria (type x zones x price x rooms). Panel only, no alerts."""
