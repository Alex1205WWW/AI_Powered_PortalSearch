"""Crawl orchestration: queued initial runs, incremental sweeps with watermarks, email polls, matching, events, flush.
Collect once, match many: a sweep covers the union of all active criteria zones, then every new or changed
listing is evaluated against every active criteria.

One ingest path for every source:  persist (listings) -> assign (properties, dedup) -> match (on the property's
merged view) -> events -> notifications."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import AppSetting, CrawlRun, Criteria, EmailMessage, Geo, Listing, PropertyMatch, Source
from app.domain import events
from app.domain.listing import CanonicalListing, FillStats
from app.services import listings as listing_svc
from app.services import matches as match_svc
from app.services import notifications as notif_svc
from app.services import health
from app.services import properties as prop_svc
from app.sources import load_adapters
from app.sources.base import is_catalogue
from app.sources.registry import ADAPTERS

log = logging.getLogger(__name__)
load_adapters()

INITIAL_SOURCES = ("imovirtual",)  # portals that support a targeted first pass for new criteria
COMMIT_EVERY = 25  # listings between commits during a catalogue pass; also how often the cursor is saved
EMAIL_SOURCE = "idealista_email"


# --- watermarks (app_settings) ------------------------------------------------------------------

def _wm_key(source_key: str, estate: str, council: str) -> str:
    return f"watermark:{source_key}:{estate}:{council}"


def _wm_key_catalogue(source_key: str) -> str:
    """Catalogue sources (RE/MAX) have one global watermark, not one per (estate, council). The key keeps the
    `watermark:<source>:` prefix so `rewind_watermarks` reaches it too."""
    return f"watermark:{source_key}:catalogue"


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


def get_setting(session: Session, key: str):
    row = session.get(AppSetting, key)
    return row.value if row else None


def set_setting(session: Session, key: str, value) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


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


# --- the ingest path ----------------------------------------------------------------------------

def ingest(session: Session, source: Source, canonical: CanonicalListing) -> listing_svc.PersistResult:
    """Persist one canonical listing and make sure it belongs to a property (dedup)."""
    result = listing_svc.persist(session, source, canonical)
    if result.listing.property_id is None:
        prop_svc.assign(session, result.listing)
    return result


def match_and_notify(session: Session, result: listing_svc.PersistResult, criteria_list: list[Criteria], *, initial: bool) -> int:
    queued = 0
    listing = result.listing
    subject = prop_svc.view_for(session, listing.property_id)
    for criteria in criteria_list:
        stored = match_svc.evaluate_and_store(session, subject, criteria)
        reasons = events.derive(result=stored.result, created=stored.created, previous_grade=stored.previous_grade,
                                listing_is_new=result.is_new, price_changed=result.price_changed,
                                old_price=result.old_price, new_price=listing.price, initial=initial)
        for reason in reasons:
            payload = {"old_price": result.old_price} if reason == "price_drop" else None
            if notif_svc.enqueue(session, buyer_id=criteria.buyer_id, property_id=listing.property_id, reason=reason,
                                 price=listing.price, payload=payload):
                queued += 1
    return queued


def _zone_filter(zones: list[str]):
    return or_(*([Listing.geo_code == z for z in zones] + [Listing.geo_code.like(f"{z}/%") for z in zones]))


def rematch_criteria(session: Session, criteria: Criteria) -> int:
    """Silently re-evaluate one criteria against every stored property in its zones (any source) plus every property
    that already has a match row for it, so rows that no longer qualify flip to `fora`. No alerts: used by initial
    runs and after data corrections or criteria edits."""
    zones = [z.geo_code for z in criteria.zones]
    if not zones:
        return 0
    in_scope = select(Listing.property_id).where(Listing.status == "active", Listing.property_id.is_not(None), _zone_filter(zones))
    if criteria.estate_types:
        in_scope = in_scope.where(or_(Listing.estate_type.is_(None), Listing.estate_type.in_(criteria.estate_types)))
    already = select(PropertyMatch.property_id).where(PropertyMatch.criteria_id == criteria.id)
    property_ids = set(session.scalars(in_scope)) | set(session.scalars(already))
    count = 0
    for property_id in sorted(property_ids):
        subject = prop_svc.view_for(session, property_id)
        if subject is not None and match_svc.evaluate_and_store(session, subject, criteria).result.ok:
            count += 1
    return count


def rematch_all(session: Session) -> dict[int, int]:
    from app.services.criteria import list_active

    return {criteria.id: rematch_criteria(session, criteria) for criteria in list_active(session)}


# --- runs ---------------------------------------------------------------------------------------

def _source(session: Session, key: str) -> Source:
    source = session.scalar(select(Source).where(Source.key == key))
    if source is None:
        raise RuntimeError(f"source {key} not seeded")
    return source


def _health_ok(source: Source) -> None:
    health.mark_ok(source)


def _health_fail(source: Source, error: str) -> None:
    health.record_failure(source, error)


def _apply_canary(source: Source, run: CrawlRun, stats: FillStats) -> None:
    """Field-fill canary (Roadmap_M1.md section 2 item 9): a breach flips the source to `degraded` at once, even though
    the run itself completed, because silently empty fields are how parser drift shows up."""
    breaches = stats.breaches()
    if breaches:
        reason = "canary: " + "; ".join(breaches)
        run.error = reason
        health.degrade(source, reason)
    else:
        _health_ok(source)


def backfill_pending(session: Session, source: Source) -> bool:
    """True while a catalogue source has never completed its historical pass."""
    done = session.scalar(select(CrawlRun.id).where(CrawlRun.source_id == source.id, CrawlRun.kind == "backfill",
                                                    CrawlRun.status == "ok").limit(1))
    started = session.scalar(select(CrawlRun.id).where(CrawlRun.source_id == source.id,
                                                       CrawlRun.kind == "backfill").limit(1))
    return bool(started) and not done


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
    session.commit()
    blocker = _can_crawl(source) if source else "fonte inexistente"
    if criteria is None or blocker:
        run.status, run.error, run.finished_at = "failed", blocker or "critérios apagados", datetime.now(timezone.utc)
        return run
    adapter = ADAPTERS[source.key](source)
    stats = FillStats()
    try:
        for estate, district, council in sorted(criteria_scope(session, criteria)):
            for canonical in adapter.iter_initial(estate, district, council, criteria.price_min, criteria.price_max):
                stats.add(canonical)
                result = ingest(session, source, canonical)
                run.listings_new += result.is_new
                run.listings_updated += not result.is_new
                match_and_notify(session, result, [criteria], initial=True)
            run.pages_fetched += 1
            session.commit()
        rematched = rematch_criteria(session, criteria)
        run.status, run.finished_at = "ok", datetime.now(timezone.utc)
        _apply_canary(source, run, stats)
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


def _sweep_query(session: Session, source: Source, adapter, run: CrawlRun, criteria_list: list[Criteria],
                 started: datetime, stats: FillStats) -> int:
    """One search per (estate x concelho), newest first, one watermark per pair. Imovirtual."""
    queued = 0
    for estate, district, council in sorted(sweep_scope(session)):
        # No watermark yet (new scope): look back one day instead of paging through the whole council.
        since = get_watermark(session, source.key, estate, council) or (started - timedelta(hours=24))
        for canonical in adapter.iter_newest(estate, district, council, since):
            stats.add(canonical)
            result = ingest(session, source, canonical)
            run.listings_new += result.is_new
            run.listings_updated += not result.is_new
            if result.is_new or result.price_changed or result.status_changed:
                queued += match_and_notify(session, result, criteria_list, initial=False)
        set_watermark(session, source.key, estate, council, started)
        run.pages_fetched += 1
        session.commit()
    return queued


def _sweep_catalogue(session: Session, source: Source, adapter, run: CrawlRun, criteria_list: list[Criteria],
                     started: datetime, stats: FillStats, *, initial: bool = False, limit: int | None = None) -> int:
    """One global catalogue filtered by its own change timestamps, one watermark. RE/MAX.

    Also serves the backfill (`initial=True`, `since=None`): the same ingest path, with alerts suppressed,
    because a historical bulk import of listings already on the market is panel material, not news."""
    key = _wm_key_catalogue(source.key)
    row = session.get(AppSetting, key)
    stored = datetime.fromisoformat(row.value) if row and isinstance(row.value, str) else None
    since = None if initial else (stored or (started - timedelta(hours=24)))
    cursor = dict(run.cursor or {})
    queued = 0
    for canonical in adapter.iter_changed(since, limit=limit, cursor=cursor, session=session):
        stats.add(canonical)
        result = ingest(session, source, canonical)
        run.listings_new += result.is_new
        run.listings_updated += not result.is_new
        if result.is_new or result.price_changed or result.status_changed:
            queued += match_and_notify(session, result, criteria_list, initial=initial)
        run.pages_fetched = int(cursor.get("index") or run.pages_fetched + 1)
        if run.pages_fetched % COMMIT_EVERY == 0:
            run.cursor = dict(cursor)
            session.commit()
    run.cursor = dict(cursor)
    # The watermark only moves on a pass that was allowed to reach the end of the catalogue; a measured
    # slice (`limit`) must not make the rest of the catalogue look already seen.
    if limit is None and not initial:
        if row is None:
            session.add(AppSetting(key=key, value=started.isoformat()))
        else:
            row.value = started.isoformat()
    return queued


def _resumable_run(session: Session, source: Source, kind: str) -> CrawlRun | None:
    """The newest unfinished run of this kind for this source, so a killed backfill continues where it
    stopped instead of reading the catalogue from the beginning."""
    return session.scalar(select(CrawlRun).where(CrawlRun.source_id == source.id, CrawlRun.kind == kind,
                                                 CrawlRun.status.in_(("running", "failed", "queued")))
                          .order_by(CrawlRun.id.desc()).limit(1))


def execute_sweep(session: Session, source_key: str, *, initial: bool = False, limit: int | None = None,
                  kind: str | None = None, resume: bool = False) -> CrawlRun:
    from app.services.criteria import list_active

    source = _source(session, source_key)
    run = _resumable_run(session, source, kind or "sweep") if resume else None
    if run is None:
        run = CrawlRun(source_id=source.id, kind=kind or "sweep", status="running")
        session.add(run)
    else:
        run.status, run.error = "running", None
        log.info("resuming %s at index %s", run.kind, (run.cursor or {}).get("index"),
                 extra={"source": source_key, "run_id": run.id})
    session.commit()  # the run row must survive a rollback so a failure can be recorded on it
    blocker = _can_crawl(source)
    if blocker:
        run.status, run.error, run.finished_at = "failed", blocker, datetime.now(timezone.utc)
        session.commit()
        return run
    adapter = ADAPTERS[source.key](source)
    if run.kind == "sweep" and is_catalogue(adapter) and backfill_pending(session, source):
        # The historical pass is still reading the same catalogue; a second crawl would only compete with it.
        run.status, run.error, run.finished_at = "ok", "backfill em curso: sweep adiado", datetime.now(timezone.utc)
        session.commit()
        return run
    criteria_list = list_active(session)
    started = datetime.now(timezone.utc)
    stats = FillStats()
    try:
        if is_catalogue(adapter):
            queued = _sweep_catalogue(session, source, adapter, run, criteria_list, started, stats,
                                      initial=initial, limit=limit)
        else:
            queued = _sweep_query(session, source, adapter, run, criteria_list, started, stats)
        run.status, run.finished_at = "ok", datetime.now(timezone.utc)
        _apply_canary(source, run, stats)
        session.commit()
        sent = notif_svc.flush(session)
        session.commit()
        log.info("%s done: new=%d updated=%d alerts queued=%d sent=%d", run.kind, run.listings_new,
                 run.listings_updated, queued, sent, extra={"source": source.key, "run_id": run.id})
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        run = session.get(CrawlRun, run.id)
        run.status, run.error, run.finished_at = "failed", repr(exc), datetime.now(timezone.utc)
        _health_fail(session.get(Source, run.source_id), repr(exc))
        session.commit()
        log.exception("%s failed", run.kind, extra={"source": source_key, "run_id": run.id})
    return run


def execute_backfill(session: Session, source_key: str, limit: int | None = None) -> CrawlRun:
    """The one-off historical pass over a catalogue source. Resumable through `crawl_runs.cursor`, and silent:
    a bulk import of listings already on the market is not news (Roadmap_M1.md section 6, Day 6 gate)."""
    return execute_sweep(source_key=source_key, session=session, initial=True, limit=limit,
                         kind="backfill", resume=True)


def execute_email_poll(session: Session) -> CrawlRun:
    """Read new Idealista alert emails, ingest their listings through the same path, alert on genuinely new ones."""
    from app.config import settings
    from app.services.criteria import list_active
    from app.sources import idealista_email as mail

    source = _source(session, EMAIL_SOURCE)
    run = CrawlRun(source_id=source.id, kind="email", status="running")
    session.add(run)
    session.commit()
    if not source.enabled or not settings.imap_user or not settings.imap_password:
        run.status, run.error, run.finished_at = "failed", "fonte desativada ou IMAP não configurado", datetime.now(timezone.utc)
        session.commit()
        return run
    mailbox_key = f"{settings.imap_user}/{settings.imap_folder}"
    criteria_list = list_active(session)
    queued = 0
    try:
        last_uid = int(get_setting(session, f"imap:last_uid:{mailbox_key}") or 0)
        known_validity = get_setting(session, f"imap:uidvalidity:{mailbox_key}")
        with mail.connect(settings) as box:
            validity = mail.uidvalidity(box, settings.imap_folder)
            if known_validity is not None and validity != known_validity:
                log.warning("imap: UIDVALIDITY changed (%s -> %s); rescanning the mailbox", known_validity, validity, extra={"source": source.key})
                last_uid = 0
            set_setting(session, f"imap:uidvalidity:{mailbox_key}", validity)
            for message in mail.fetch_after(box, last_uid):
                uid = int(message.uid)
                cards = mail.cards_from_message(message)
                found = 0
                for card in cards:
                    canonical = mail.to_canonical(card, received_at=message.date, resolve=lambda tokens: mail.resolve_zone(session, tokens))
                    result = ingest(session, source, canonical)
                    run.listings_new += result.is_new
                    run.listings_updated += not result.is_new
                    found += 1
                    if result.is_new or result.price_changed:
                        queued += match_and_notify(session, result, criteria_list, initial=False)
                session.merge(EmailMessage(mailbox=mailbox_key[:120], uid=uid, received_at=message.date, parsed_ok=True, listing_urls_found=found))
                last_uid = max(last_uid, uid)
                set_setting(session, f"imap:last_uid:{mailbox_key}", last_uid)
                run.pages_fetched += 1
                session.commit()
        run.status, run.finished_at = "ok", datetime.now(timezone.utc)
        _health_ok(source)
        session.commit()
        sent = notif_svc.flush(session)
        session.commit()
        if run.pages_fetched:
            log.info("email poll done: messages=%d new=%d updated=%d alerts queued=%d sent=%d", run.pages_fetched, run.listings_new,
                     run.listings_updated, queued, sent, extra={"source": source.key, "run_id": run.id})
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        run = session.get(CrawlRun, run.id)
        run.status, run.error, run.finished_at = "failed", repr(exc), datetime.now(timezone.utc)
        _health_fail(session.get(Source, run.source_id), repr(exc))
        session.commit()
        log.exception("email poll failed", extra={"source": EMAIL_SOURCE, "run_id": run.id})
    return run


def run_queued(session: Session) -> int:
    done = 0
    for run in list(session.scalars(select(CrawlRun).where(CrawlRun.status == "queued").order_by(CrawlRun.id))):
        if run.kind == "initial":
            execute_initial(session, run)
            done += 1
    return done
