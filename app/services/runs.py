"""Crawl orchestration: queued initial runs, incremental sweeps with watermarks, matching, events, flush.
Collect once, match many: a sweep covers the union of all active criteria zones, then every new or changed
listing is evaluated against every active criteria."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSetting, CrawlRun, Criteria, Geo, Listing, Source
from app.domain import events
from app.domain.listing import CanonicalListing
from app.services import listings as listing_svc
from app.services import matches as match_svc
from app.services import notifications as notif_svc
from app.sources import load_adapters
from app.sources.registry import ADAPTERS

log = logging.getLogger(__name__)
load_adapters()

INITIAL_SOURCES = ("imovirtual",)  # portals that support a targeted first pass for new criteria


# --- watermarks (app_settings) ------------------------------------------------------------------

def _wm_key(source_key: str, estate: str, council: str) -> str:
    return f"watermark:{source_key}:{estate}:{council}"


def get_watermark(session: Session, source_key: str, estate: str, council: str) -> datetime | None:
    row = session.get(AppSetting, _wm_key(source_key, estate, council))
    return datetime.fromisoformat(row.value) if row and isinstance(row.value, str) else None


def set_watermark(session: Session, source_key: str, estate: str, council: str, when: datetime) -> None:
    key = _wm_key(source_key, estate, council)
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=when.isoformat()))
    else:
        row.value = when.isoformat()


def rewind_watermarks(session: Session, source_key: str, hours: float) -> int:
    rows = list(session.scalars(select(AppSetting).where(AppSetting.key.like(f"watermark:{source_key}:%"))))
    for row in rows:
        row.value = (datetime.fromisoformat(row.value) - timedelta(hours=hours)).isoformat()
    return len(rows)


# --- scope --------------------------------------------------------------------------------------

def expand_zone(session: Session, code: str) -> list[tuple[str, str]]:
    """Zone code -> [(district, council)] pairs a portal can be queried with."""
    geo = session.get(Geo, code)
    if geo is None:
        return []
    if geo.level == "district":
        return [(code, c.code.split("/")[1]) for c in session.scalars(select(Geo).where(Geo.parent_code == code, Geo.level == "council"))]
    parts = code.split("/")
    return [(parts[0], parts[1])] if len(parts) >= 2 else []


def criteria_scope(session: Session, criteria: Criteria) -> set[tuple[str, str, str]]:
    scope: set[tuple[str, str, str]] = set()
    for zone in criteria.zones:
        for district, council in expand_zone(session, zone.geo_code):
            for estate in criteria.estate_types:
                if estate in ("apartamento", "moradia"):
                    scope.add((estate, district, council))
    return scope


def sweep_scope(session: Session) -> set[tuple[str, str, str]]:
    from app.services.criteria import list_active

    scope: set[tuple[str, str, str]] = set()
    for criteria in list_active(session):
        scope |= criteria_scope(session, criteria)
    return scope


# --- matching + events after persistence ---------------------------------------------------------

def match_and_notify(session: Session, result: listing_svc.PersistResult, criteria_list: list[Criteria], *, initial: bool) -> int:
    queued = 0
    listing = result.listing
    for criteria in criteria_list:
        stored = match_svc.evaluate_and_store(session, listing, criteria)
        reasons = events.derive(result=stored.result, created=stored.created, previous_grade=stored.previous_grade,
                                listing_is_new=result.is_new, price_changed=result.price_changed,
                                old_price=result.old_price, new_price=listing.price, initial=initial)
        for reason in reasons:
            payload = {"old_price": result.old_price} if reason == "price_drop" else None
            if notif_svc.enqueue(session, buyer_id=criteria.buyer_id, property_id=listing.property_id, reason=reason,
                                 price=listing.price, payload=payload):
                queued += 1
    return queued


def rematch_criteria(session: Session, criteria: Criteria) -> int:
    """Evaluate everything already stored (any source) against one criteria. Used by initial runs so a new
    buyer immediately benefits from RE/MAX, email and agency listings collected for others. No alerts."""
    zones = [z.geo_code for z in criteria.zones]
    if not zones:
        return 0
    conds = [Listing.geo_code == z for z in zones] + [Listing.geo_code.like(f"{z}/%") for z in zones]
    from sqlalchemy import or_

    stmt = select(Listing).where(Listing.status == "active", or_(*conds))
    if criteria.estate_types:
        stmt = stmt.where(Listing.estate_type.in_(criteria.estate_types))
    count = 0
    for listing in session.scalars(stmt):
        if match_svc.evaluate_and_store(session, listing, criteria).result.ok:
            count += 1
    return count


# --- runs ---------------------------------------------------------------------------------------

def _source(session: Session, key: str) -> Source:
    source = session.scalar(select(Source).where(Source.key == key))
    if source is None:
        raise RuntimeError(f"source {key} not seeded")
    return source


def _health_ok(source: Source) -> None:
    source.consecutive_failures = 0
    source.last_ok_at = datetime.now(timezone.utc)
    source.last_error = None
    if source.status == "degraded":
        source.status = "ok"


def _health_fail(source: Source, error: str) -> None:
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    source.last_error = error[:2000]
    if source.consecutive_failures >= 2 and source.status == "ok":
        source.status = "degraded"


def _can_crawl(source: Source) -> str | None:
    if not source.enabled:
        return "fonte desativada"
    if source.status == "blocked" or source.robots_allowed is False:
        return "robots.txt não permite"
    if source.key not in ADAPTERS:
        return "sem adaptador"
    return None


def queue_initial(session: Session, criteria: Criteria) -> list[CrawlRun]:
    runs = []
    for key in INITIAL_SOURCES:
        source = session.scalar(select(Source).where(Source.key == key, Source.enabled.is_(True)))
        if source is None:
            continue
        run = CrawlRun(source_id=source.id, kind="initial", criteria_id=criteria.id, status="queued")
        session.add(run)
        runs.append(run)
    session.flush()
    return runs


def execute_initial(session: Session, run: CrawlRun) -> CrawlRun:
    source = session.get(Source, run.source_id)
    criteria = session.get(Criteria, run.criteria_id)
    run.status, run.started_at = "running", datetime.now(timezone.utc)
    session.flush()
    blocker = _can_crawl(source) if source else "fonte inexistente"
    if criteria is None or blocker:
        run.status, run.error, run.finished_at = "failed", blocker or "critérios apagados", datetime.now(timezone.utc)
        return run
    adapter = ADAPTERS[source.key](source)
    try:
        for estate, district, council in sorted(criteria_scope(session, criteria)):
            for canonical in adapter.iter_initial(estate, district, council, criteria.price_min, criteria.price_max):
                result = listing_svc.persist(session, source, canonical)
                run.listings_new += result.is_new
                run.listings_updated += not result.is_new
                match_and_notify(session, result, [criteria], initial=True)
            run.pages_fetched += 1
            session.commit()
        rematched = rematch_criteria(session, criteria)
        run.status, run.finished_at = "ok", datetime.now(timezone.utc)
        _health_ok(source)
        log.info("initial run done: criteria=%s new=%d updated=%d rematched=%d", criteria.id, run.listings_new, run.listings_updated, rematched,
                 extra={"source": source.key, "run_id": run.id, "criteria_id": criteria.id})
    except Exception as exc:  # noqa: BLE001 - a run must always close with a status
        session.rollback()
        run = session.get(CrawlRun, run.id)
        run.status, run.error, run.finished_at = "failed", repr(exc), datetime.now(timezone.utc)
        _health_fail(session.get(Source, run.source_id), repr(exc))
        log.exception("initial run failed", extra={"source": source.key, "run_id": run.id})
    session.commit()
    return run


def execute_sweep(session: Session, source_key: str) -> CrawlRun:
    from app.services.criteria import list_active

    source = _source(session, source_key)
    run = CrawlRun(source_id=source.id, kind="sweep", status="running")
    session.add(run)
    session.flush()
    blocker = _can_crawl(source)
    if blocker:
        run.status, run.error, run.finished_at = "failed", blocker, datetime.now(timezone.utc)
        session.commit()
        return run
    adapter = ADAPTERS[source.key](source)
    criteria_list = list_active(session)
    started = datetime.now(timezone.utc)
    queued = 0
    try:
        for estate, district, council in sorted(sweep_scope(session)):
            # No watermark yet (new scope): look back one day instead of paging through the whole council.
            since = get_watermark(session, source.key, estate, council) or (started - timedelta(hours=24))
            for canonical in adapter.iter_newest(estate, district, council, since):
                result = listing_svc.persist(session, source, canonical)
                run.listings_new += result.is_new
                run.listings_updated += not result.is_new
                if result.is_new or result.price_changed or result.status_changed:
                    queued += match_and_notify(session, result, criteria_list, initial=False)
            set_watermark(session, source.key, estate, council, started)
            run.pages_fetched += 1
            session.commit()
        run.status, run.finished_at = "ok", datetime.now(timezone.utc)
        _health_ok(source)
        session.commit()
        sent = notif_svc.flush(session)
        session.commit()
        log.info("sweep done: new=%d updated=%d alerts queued=%d sent=%d", run.listings_new, run.listings_updated, queued, sent,
                 extra={"source": source.key, "run_id": run.id})
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        run = session.get(CrawlRun, run.id)
        run.status, run.error, run.finished_at = "failed", repr(exc), datetime.now(timezone.utc)
        _health_fail(session.get(Source, run.source_id), repr(exc))
        session.commit()
        log.exception("sweep failed", extra={"source": source_key, "run_id": run.id})
    return run


def run_queued(session: Session) -> int:
    done = 0
    for run in list(session.scalars(select(CrawlRun).where(CrawlRun.status == "queued").order_by(CrawlRun.id))):
        if run.kind == "initial":
            execute_initial(session, run)
            done += 1
    return done
