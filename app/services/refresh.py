"""Daily refresh of the matched set (Roadmap_M1.md §3 item 7, §6 Day 4).

Search results and sitemaps are cheap but shallow. The detail page is where the lift, the garage, the land
area, the exact price and "this listing is gone" live. Re-reading every listing we store would be tens of
thousands of requests a day; re-reading only the ones that actually matter to a buyer is a few hundred.

So: every property that currently matches at least one active criteria gets its listings re-read once a day,
through the ordinary ingest path, which means price history, removals and newly stated features arrive as
the normal events and alerts rather than as a special case.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Criteria, Listing, PropertyMatch, Source
from app.services import listings as listing_svc
from app.services import runs as run_svc
from app.sources.base import can_refresh
from app.sources.registry import ADAPTERS

log = logging.getLogger(__name__)

MAX_PER_RUN = 1500  # a safety ceiling; the matched set is in the hundreds


@dataclass
class RefreshStats:
    checked: int = 0
    updated: int = 0
    removed: int = 0
    price_changes: int = 0
    alerts: int = 0
    failed: int = 0

    def as_dict(self) -> dict:
        return {"checked": self.checked, "updated": self.updated, "removed": self.removed,
                "price_changes": self.price_changes, "alerts": self.alerts, "failed": self.failed}


def matched_listings(session: Session, limit: int = MAX_PER_RUN) -> list[Listing]:
    """Listings belonging to a property that matches at least one active criteria."""
    property_ids = select(PropertyMatch.property_id).join(Criteria, Criteria.id == PropertyMatch.criteria_id).where(
        PropertyMatch.grade != "fora", Criteria.active.is_(True))
    stmt = (select(Listing)
            .where(Listing.property_id.in_(property_ids), Listing.status == "active")
            .order_by(Listing.last_seen_at)
            .limit(limit))
    return list(session.scalars(stmt))


def refresh_matched(session: Session, limit: int = MAX_PER_RUN) -> dict:
    """Re-read the matched set one listing at a time, committing as we go so an interrupted run keeps its work."""
    from app.services.criteria import list_active
    from app.services import notifications as notif_svc

    stats = RefreshStats()
    criteria_list = list_active(session)
    sources = {s.id: s for s in session.scalars(select(Source))}
    adapters: dict[int, object] = {}
    for listing in matched_listings(session, limit):
        source = sources.get(listing.source_id)
        if source is None or source.key not in ADAPTERS or not source.enabled:
            continue
        adapter = adapters.get(source.id) or ADAPTERS[source.key](source)
        adapters[source.id] = adapter
        if not can_refresh(adapter):
            continue  # e.g. the email source: there is no page to go back to
        stats.checked += 1
        try:
            canonical = adapter.refresh_listing(session, listing)
        except Exception as exc:  # noqa: BLE001 - one dead page must not end the pass
            stats.failed += 1
            log.warning("refresh failed for listing %s: %r", listing.id, exc, extra={"source": source.key})
            session.rollback()
            continue
        if canonical is None:
            listing_svc.mark_removed(session, listing)
            stats.removed += 1
            session.commit()
            continue
        result = run_svc.ingest(session, source, canonical)
        stats.updated += 1
        stats.price_changes += bool(result.price_changed)
        # Always re-match, not only on a price or status change: the point of the refresh is the fields the
        # search result never carried, and a newly stated lift turns "possível" into "confirmada", which is
        # the `data_completed` event.
        stats.alerts += run_svc.match_and_notify(session, result, criteria_list, initial=False)
        session.commit()
    sent = notif_svc.flush(session)
    session.commit()
    log.info("matched refresh sent %d alerts", sent)
    return stats.as_dict()
