"""Contract every source adapter implements. The worker only talks to adapters through these protocols.

There are two shapes, because the two portals are genuinely different and bending one into the other
would cost a sitemap re-read per council:

  scope_mode = "query"      the source is asked one question per (estate type x concelho), newest first,
                            and each pair carries its own watermark.   Imovirtual.
  scope_mode = "catalogue"  the source publishes one global catalogue (a sitemap) that already encodes
                            type, transaction and location, so the filtering happens before any fetch and
                            a single watermark covers the source.      RE/MAX.

A "query" adapter also supports `iter_initial`, the targeted first pass for newly created criteria.
A "catalogue" adapter cannot: its catalogue is not queryable by price. New criteria are served from what
the backfill already stored, by `services.runs.rematch_criteria`, which re-evaluates every stored property
in the criteria's zones regardless of source.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from app.db.models import Source
from app.domain.listing import CanonicalListing

ScopeMode = Literal["query", "catalogue"]


@runtime_checkable
class SourceAdapter(Protocol):
    key: str
    scope_mode: ScopeMode

    def __init__(self, source: Source) -> None: ...


@runtime_checkable
class QueryAdapter(SourceAdapter, Protocol):
    """Per (estate x concelho) querying, newest first."""

    def iter_newest(self, estate: str, district: str, council: str, since: datetime | None) -> Iterator[CanonicalListing]:
        """Listings published after `since`, newest first. The caller stores the watermark per (estate, council)."""

    def iter_initial(self, estate: str, district: str, council: str,
                     price_min: int | None, price_max: int | None) -> Iterator[CanonicalListing]:
        """Targeted first pass for new criteria. Panel only, never alerts."""


@runtime_checkable
class CatalogueAdapter(SourceAdapter, Protocol):
    """One global catalogue with its own change timestamps."""

    def iter_changed(self, since: datetime | None, *, limit: int | None = None,
                     cursor: dict | None = None) -> Iterator[CanonicalListing]:
        """Listings whose catalogue entry changed after `since`, in catalogue order.

        `cursor` resumes an interrupted pass; the adapter updates it in place so the caller can persist it on
        `crawl_runs.cursor` after every commit. `limit` bounds one slice (used by the measured backfill)."""


def is_catalogue(adapter: object) -> bool:
    return getattr(adapter, "scope_mode", "query") == "catalogue"
