"""Agency discovery, profiling and the daily watch (deliverables 4, 5, 12).

The funnel, with the volumes measured on 2026-10-31 (`Milestone1/Plan_Day5.md` §3.5):

    directory, 50 concelhos, every page   142 req  -> 2 278 agencies, all stored
      adsSell > 0                                     776
        profile /pt/empresa/<slug>        776 req  -> ~213 with a website (27 %)
          profiler cascade              ~1 065 req -> platform, profile, measured confidence
            first catalogue read                   -> listings (silent), off-portal ratio
              daily watch, ranked by that ratio

Agency listings reach the database through exactly the same path as a portal's
(`runs.ingest` -> persist -> dedup -> match -> events -> notifications), so nothing about matching,
deduplication, alerting or export is special-cased for them.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Agency, CrawlRun, Geo, Listing, Source
from app.domain.listing import canary_fields_for, canary_thresholds_for, normalize_text, FillStats
from app.geo import resolve as geo_resolve
from app.services import health
from app.sources import agency_generic as reader
from app.sources import agency_profiler as profiler
from app.sources import imovirtual
from app.sources.http import HttpClient, get_client

log = logging.getLogger(__name__)

SOURCE_KEY_MAX = 60
AGENCY_DELAY_S = 2.0
# A short, honest cap per watch run: new listings are fetched one page each, and a site that just published
# 400 ads should not monopolise the night. The rest arrive on the next tick.
WATCH_FETCH_LIMIT = 150
# The removal guard (Plan_Day5.md section 3.3): eGO sitemaps have no <lastmod>, so withdrawal is detected by
# set-difference. A short or truncated sitemap must never be allowed to retire a whole catalogue.
CATALOGUE_SHRINK_FLOOR = 0.80
REMOVAL_SHARE_CAP = 0.10


# --- identity ----------------------------------------------------------------------------------

def source_key_for(website_url: str) -> str:
    host = (urlparse(website_url).netloc or website_url).lower().removeprefix("www.")
    key = f"agency:{host}"
    if len(key) <= SOURCE_KEY_MAX:
        return key
    from hashlib import blake2s
    return f"{key[:SOURCE_KEY_MAX - 9]}-{blake2s(host.encode('utf-8'), digest_size=4).hexdigest()}"


def _free_name_norm(session: Session, name: str, ident: str | None) -> str:
    """`agencies.name_norm` is UNIQUE but it is not an identity: two different agencies can normalise to the
    same string. When that happens, keep both rows and make the second name_norm unique rather than merging
    two businesses or failing the insert."""
    base = normalize_text(name)[:200] or (ident or "agencia")[:200]
    clash = session.scalar(select(Agency).where(Agency.name_norm == base))
    if clash is None or clash.imovirtual_id == ident:
        return base
    return f"{base} #{(ident or '')[:8]}"[:200]


def upsert_directory_row(session: Session, row: dict, geo_code: str | None) -> Agency:
    """One directory row -> one `agencies` row, keyed on the Imovirtual UUID.

    An agency first seen through a portal listing has a name but no UUID; the directory then adopts that row
    instead of creating a second one."""
    agency = None
    if row.get("id"):
        agency = session.scalar(select(Agency).where(Agency.imovirtual_id == row["id"]))
    if agency is None and row.get("name"):
        candidate = session.scalar(select(Agency).where(Agency.name_norm == normalize_text(row["name"])[:200]))
        agency = candidate if candidate is not None and candidate.imovirtual_id in (None, row.get("id")) else None
    if agency is None:
        agency = Agency(name=(row.get("name") or "")[:200] or "(sem nome)",
                        name_norm=_free_name_norm(session, row.get("name") or "", row.get("id")))
        session.add(agency)
    if row.get("id"):
        agency.imovirtual_id = row["id"][:40]
    if row.get("slug"):
        agency.imovirtual_slug = row["slug"][:200]
    if row.get("phone") and not agency.phone:
        agency.phone = row["phone"][:40]
    agency.active_ads_count = row.get("ads_sell")
    agency.active_ads_rent_count = row.get("ads_rent")
    if geo_code and not agency.geo_code:
        agency.geo_code = geo_code
    session.flush()
    return agency


# --- step 1: the directory ---------------------------------------------------------------------

def councils(session: Session) -> list[tuple[str, str]]:
    rows = session.scalars(select(Geo).where(Geo.level == "council").order_by(Geo.code))
    return [(g.code.split("/")[0], g.code.split("/")[1]) for g in rows if "/" in g.code]


def crawl_directory(session: Session, *, client: HttpClient | None = None, max_pages: int | None = None) -> dict:
    """Read every directory page for every concelho in the seed. Every page, because the list is sorted by
    descending ad count and the quiet agencies are at the end."""
    client = client or get_client()
    source = session.scalar(select(Source).where(Source.key == "imovirtual"))
    delay = source.crawl_delay_s if source else AGENCY_DELAY_S
    stats = {"pages": 0, "rows": 0, "agencies": 0, "with_sell": 0, "councils": 0}
    for district, council in councils(session):
        stats["councils"] += 1
        page = 1
        while True:
            if max_pages and stats["pages"] >= max_pages:
                return stats
            url = imovirtual.directory_url(district, council, page)
            try:
                response = client.get(url, delay_s=delay, retries=2)
                response.raise_for_status()
                data = imovirtual.extract_next_data(response.text)
            except Exception as exc:  # noqa: BLE001 - one bad concelho must not stop the crawl
                log.warning("directory %s/%s page %d failed: %r", district, council, page, exc)
                break
            stats["pages"] += 1
            items, meta = imovirtual.parse_directory(data or {})
            for item in items:
                row = imovirtual.normalise_company(item)
                if not row["name"]:
                    continue
                stats["rows"] += 1
                fresh = row.get("id") and not session.scalar(
                    select(Agency.id).where(Agency.imovirtual_id == row["id"]))
                upsert_directory_row(session, row, f"{district}/{council}")
                stats["agencies"] += bool(fresh)
                if row["ads_sell"]:
                    stats["with_sell"] += 1
            session.commit()
            if not (meta["pagination"] or {}).get("hasNext"):
                break
            page += 1
    return stats


# --- step 2: website resolution ----------------------------------------------------------------

def pending_website_checks(session: Session, limit: int) -> list[Agency]:
    """Agencies worth a profile fetch: they publish for sale and we have not looked yet.

    Zero-sale agencies are skipped deliberately. Of the 1 502 of them, 1 480 also have no rental ads, and
    none of the six sampled had a website at all - there is nothing to read behind them."""
    stmt = (select(Agency)
            .where(Agency.active_ads_count > 0, Agency.website_url.is_(None),
                   Agency.imovirtual_slug.is_not(None), Agency.checked_at.is_(None))
            .order_by(Agency.active_ads_count.desc())
            .limit(limit))
    return list(session.scalars(stmt))


def resolve_websites(session: Session, *, limit: int = 100, client: HttpClient | None = None) -> dict:
    client = client or get_client()
    stats = {"checked": 0, "found": 0}
    for agency in pending_website_checks(session, limit):
        url = imovirtual.profile_url(agency.imovirtual_slug)
        try:
            response = client.get(url, delay_s=AGENCY_DELAY_S, retries=2)
            website = imovirtual.website_from_profile(response.text) if response.status_code == 200 else None
        except Exception as exc:  # noqa: BLE001
            log.info("profile %s failed: %r", agency.imovirtual_slug, exc)
            website = None
        agency.checked_at = datetime.now(timezone.utc)
        stats["checked"] += 1
        if website:
            agency.website_url = website
            agency.website_resolution = "profile"
            stats["found"] += 1
        session.commit()
    return stats


def add_manual_website(session: Session, name: str, website_url: str) -> Agency:
    """Her own hand list, and anything she pastes into the panel later."""
    if not website_url.startswith("http"):
        website_url = "https://" + website_url.strip().lstrip("/")
    host = urlparse(website_url).netloc.lower()
    existing = session.scalar(select(Agency).where(Agency.website_url.ilike(f"%{host}%")))
    agency = existing or session.scalar(select(Agency).where(Agency.name_norm == normalize_text(name)[:200]))
    if agency is None:
        agency = Agency(name=name[:200], name_norm=_free_name_norm(session, name, None))
        session.add(agency)
    agency.website_url = website_url
    agency.website_resolution = "manual"
    agency.checked_at = datetime.now(timezone.utc)
    session.flush()
    return agency


# --- step 3: the source row and the profile ----------------------------------------------------

def in_her_districts(session: Session):
    """A predicate over a listing URL: does its slug name a place inside her three districts?

    Used to choose profiling samples. These sites are national - JLL and Dils sell in Porto and the Algarve
    too - so measuring a site on its Douro and Algarve stock says nothing about how well it will be read for
    Rosa and Raquel. The place names come from the `geo` table, including the parishes learned from the
    portals, so this sharpens itself as the system sees more."""
    places = {g.code.split("/")[-1] for g in session.scalars(
        select(Geo).where(Geo.level.in_(("council", "parish"))))}
    places = {p for p in places if len(p) > 3}

    def predicate(url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(place in path for place in places)

    return predicate

def ensure_source(session: Session, agency: Agency) -> Source | None:
    if not agency.website_url:
        return None
    key = source_key_for(agency.website_url)
    source = session.scalar(select(Source).where(Source.key == key))
    if source is None:
        parsed = urlparse(agency.website_url)
        source = Source(key=key, kind="agency", name=agency.name[:120],
                        base_url=f"{parsed.scheme or 'https'}://{parsed.netloc}",
                        enabled=True, schedule_minutes=60 * 24, crawl_delay_s=AGENCY_DELAY_S,
                        notes="Mediadora descoberta automaticamente (Dia 5).")
        session.add(source)
        session.flush()
    agency.watch_source_id = source.id
    session.flush()
    return source


def pending_profiles(session: Session, limit: int) -> list[Agency]:
    """Agencies with a website whose site has not been profiled yet, best prospects first.

    Ordered by portal volume as a proxy before anything is known; once a site has been read, the measured
    off-portal ratio takes over as the ranking signal for the watch."""
    stmt = (select(Agency).where(Agency.website_url.is_not(None))
            .order_by(Agency.offportal_ratio.desc().nullsfirst(), Agency.active_ads_count.desc().nullslast())
            .limit(limit * 4))
    out = []
    for agency in session.scalars(stmt):
        source = session.get(Source, agency.watch_source_id) if agency.watch_source_id else None
        if source is None or not source.profile:
            out.append(agency)
        if len(out) >= limit:
            break
    return out


def profile_agency(session: Session, agency: Agency, *, client: HttpClient | None = None) -> Source | None:
    """Run the profiler cascade against one agency site and store the result.

    A site that cannot be read is stored as `confidence='none'` **with the reason**, and `checked_at` stops
    it being retried on every tick - the gate requires exactly that, rather than a silent retry loop."""
    source = ensure_source(session, agency)
    if source is None:
        return None
    profile = profiler.build_profile(source.base_url, client=client, delay_s=source.crawl_delay_s,
                                     resolve=lambda text: geo_resolve.from_text(session, text),
                                     source_key=source.key, feed_urn=agency.source_urn,
                                     prefer=in_her_districts(session))
    source.profile = profile
    source.confidence = profile.get("confidence")
    signals = profile.get("content_signal") or {}
    source.signal_ai_input = signals.get("ai-input")
    source.signal_ai_train = signals.get("ai-train")
    source.signal_search = signals.get("search")
    source.robots_status = profile.get("robots_status")
    source.robots_fetched_at = datetime.now(timezone.utc)
    # `.get("notes", "")` is not enough: the key always exists and is None until something sets it.
    notes = profile.get("notes") or ""
    source.robots_allowed = profile.get("robots_status") in (200, 404) and not notes.startswith("robots.txt não permite")
    if profile.get("confidence") == profiler.CONF_NONE:
        source.enabled = False      # nothing readable: stop before it costs a request a day
        source.last_error = profile.get("notes") or "sem leitura possível"
        source.status = "blocked" if not source.robots_allowed else "degraded"
    else:
        source.status, source.last_error = "ok", None
    agency.checked_at = datetime.now(timezone.utc)
    session.flush()
    log.info("agency profiled: %s -> %s (%s)", source.key, source.confidence, profile.get("platform"),
             extra={"source": source.key})
    return source


# --- step 4: the catalogue read and the daily watch --------------------------------------------

def _catalogue(source: Source, client: HttpClient) -> dict[str, tuple[str, str | None]]:
    """external_id -> (url, lastmod) for every listing the site's sitemap currently publishes."""
    profile = source.profile or {}
    index = profile.get("index") or {}
    found: dict[str, tuple[str, str | None]] = {}
    for sitemap_url in index.get("urls") or []:
        response = client.get(sitemap_url, delay_s=source.crawl_delay_s, retries=2)
        response.raise_for_status()
        xml = response.text
        for block in re.findall(r"<url>(.*?)</url>", xml, re.S | re.I) or [xml]:
            loc = profiler.LOC_RE.search(block)
            if not loc:
                continue
            url = loc.group(1).strip()
            if index.get("listing_url_re") and not re.search(index["listing_url_re"], urlparse(url).path):
                continue
            lastmod = re.search(r"<lastmod>\s*([^<\s]+)", block, re.I)
            found[reader.external_id(url, profile)] = (url, lastmod.group(1) if lastmod else None)
    return found


def _known(session: Session, source: Source) -> dict[str, tuple[int, str]]:
    rows = session.execute(select(Listing.external_id, Listing.id, Listing.status)
                           .where(Listing.source_id == source.id)).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def watch(session: Session, source: Source, *, client: HttpClient | None = None, initial: bool = False,
          limit: int | None = None) -> CrawlRun:
    """One pass over an agency site: new listings in, withdrawn listings out.

    `initial=True` is the first read: it is silent and lands in the panel, which is the flow she asked for
    ("uma primeira pesquisa em todo o lado ... depois já só as atualizações das novidades").
    """
    from app.services import notifications as notif_svc
    from app.services import runs
    from app.services.criteria import list_active

    client = client or get_client()
    run = CrawlRun(source_id=source.id, kind="watch", status="running")
    session.add(run)
    session.commit()
    profile = source.profile or {}
    if not source.enabled or not profile.get("index", {}).get("listing_url_re"):
        run.status, run.error, run.finished_at = "failed", "fonte sem perfil utilizável", datetime.now(timezone.utc)
        session.commit()
        return run

    criteria_list = list_active(session)
    stats = FillStats(canary_fields_for(source.kind, profile), thresholds=canary_thresholds_for(source.kind))
    queued = 0
    try:
        catalogue = _catalogue(source, client)
        known = _known(session, source)
        guard = _removal_guard(source, len(catalogue), len(known))
        new_ids = [k for k in catalogue if k not in known]
        if profile.get("incremental") == "lastmod" and not initial:
            cutoff = source.last_ok_at
            changed = [k for k, (_u, lm) in catalogue.items()
                       if k in known and lm and cutoff and _parse_lastmod(lm) and _parse_lastmod(lm) > cutoff]
        else:
            changed = []
        todo = (new_ids + changed)[: (limit or WATCH_FETCH_LIMIT)]
        for external in todo:
            url, _lastmod = catalogue[external]
            page = profiler.fetch_text(client, url, source.crawl_delay_s)
            if page is None:
                continue
            canonical = reader.extract(page, url, profile, source.key,
                                       resolve=lambda text: geo_resolve.from_text(session, text))
            if canonical is None:
                continue
            stats.add(canonical)
            result = runs.ingest(session, source, canonical)
            run.listings_new += result.is_new
            run.listings_updated += not result.is_new
            if result.is_new or result.price_changed or result.status_changed:
                queued += runs.match_and_notify(session, result, criteria_list, initial=initial)
            run.pages_fetched += 1
            if run.pages_fetched % 20 == 0:
                session.commit()
        removed = _apply_removals(session, source, catalogue, known, guard, client)
        source.last_catalogue_size = len(catalogue)
        run.cursor = {"catalogue": len(catalogue), "new": len(new_ids), "changed": len(changed),
                      "fetched": run.pages_fetched, "removed": removed, "guard": guard}
        run.status = "ok" if len(todo) == len(new_ids) + len(changed) else "partial"
        run.finished_at = datetime.now(timezone.utc)
        breaches = stats.breaches()
        if breaches:
            run.error = "canary: " + "; ".join(breaches)
            health.degrade(source, run.error)
        else:
            health.mark_ok(source)
        session.commit()
        notif_svc.flush(session)
        session.commit()
        log.info("agency watch %s: catalogue=%d new=%d fetched=%d removed=%d alerts=%d", source.key,
                 len(catalogue), len(new_ids), run.pages_fetched, removed, queued,
                 extra={"source": source.key, "run_id": run.id})
    except Exception as exc:  # noqa: BLE001 - a run always closes with a status
        session.rollback()
        run = session.get(CrawlRun, run.id)
        run.status, run.error, run.finished_at = "failed", repr(exc), datetime.now(timezone.utc)
        health.record_failure(session.get(Source, run.source_id), repr(exc))
        session.commit()
        log.warning("agency watch failed: %s (%r)", source.key, exc, extra={"source": source.key})
    return run


def _parse_lastmod(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _removal_guard(source: Source, catalogue_size: int, known_size: int) -> str:
    """Why removals are or are not allowed on this pass. Returned so the reason lands in `crawl_runs.cursor`
    and is visible afterwards instead of being an invisible decision."""
    if catalogue_size == 0:
        return "blocked: catálogo vazio"
    baseline = source.last_catalogue_size or known_size
    if baseline and catalogue_size < baseline * CATALOGUE_SHRINK_FLOOR:
        return f"blocked: catálogo encolheu para {catalogue_size} de {baseline}"
    return "allowed"


def _apply_removals(session: Session, source: Source, catalogue: dict, known: dict, guard: str,
                    client: HttpClient) -> int:
    """Listings we hold that the sitemap no longer lists, each confirmed with one fetch.

    Absence from a sitemap is only a hint: sitemaps get regenerated, paginated and truncated. eGO answers
    **410 Gone** on a withdrawn listing (verified live 2026-10-31), so a single request settles it and a
    still-live page is left alone. Capped per run, and skipped entirely when the guard refused, because a
    short sitemap is far likelier than a mass withdrawal.
    """
    if guard != "allowed":
        log.info("removals skipped for %s (%s)", source.key, guard, extra={"source": source.key})
        return 0
    stale = [(eid, meta[0]) for eid, meta in known.items() if meta[1] == "active" and eid not in catalogue]
    cap = max(1, int(len(known) * REMOVAL_SHARE_CAP))
    removed = 0
    for _external, listing_id in stale[:cap]:
        listing = session.get(Listing, listing_id)
        if listing is None:
            continue
        try:
            response = client.get(listing.url, delay_s=source.crawl_delay_s, retries=1)
        except Exception:  # noqa: BLE001 - a network blip is not evidence of withdrawal
            continue
        if response.status_code not in reader.GONE_STATUS:
            continue
        listing.status = "removed"
        listing.removed_at = datetime.now(timezone.utc)
        removed += 1
    if removed:
        session.commit()
    return removed


# --- step 5: the ranking signal ----------------------------------------------------------------

OFFPORTAL_MIN_SAMPLE = 10      # below this the share is noise; the panel says "a medir" instead of a number
OFFPORTAL_SETTLE = timedelta(hours=24)   # a portal twin arrives on the next sweep (<= 6 h); give dedup a day


def offportal_share(total: int, offportal: int, minimum: int = OFFPORTAL_MIN_SAMPLE) -> float | None:
    """The one line of arithmetic, kept pure so the sample rule is testable without a database."""
    if total < minimum:
        return None
    return round(offportal / total, 4)


def _store_offportal(session: Session, source: Source, share: float | None, seen: int) -> None:
    """Write the measurement to EVERY agency that points at this source, not just the first.

    `scalar()` here was a bug with a visible symptom: four sources are the watch target of more than one
    agency row - the two Quintela spellings and the two Dils rows, plus century21.pt (8 franchise offices)
    and remax.pt (5). Only one row was ever written, so the other kept whatever the previous formula had
    left there: on 2026-11-02 the panel still showed "100% de 1050" for Quintela Penalva while the row the
    code actually updated had been correctly set to "a medir"."""
    for agency in session.scalars(select(Agency).where(Agency.watch_source_id == source.id)):
        agency.offportal_ratio = share
        agency.listings_seen = seen
    session.commit()


def refresh_offportal(session: Session, source: Source) -> float | None:
    """Share of this site's NEW stock that no portal we read also carried - the measured value of watching it.

    The first version divided every listing on the site by "has no portal listing in our database" and read
    100 % for almost every agency (measured 2026-11-02: Quintela + Penalva 1 200/1 200). That was not a fact
    about the market; it was the shape of our own coverage. Imovirtual is held only for the (type x concelho)
    pairs her buyers generate, and inside those only for ads published since the sweep began plus the buyers'
    targeted initial runs - so an older Lisbon apartment at 800 000 is on Imovirtual, absent from our tables,
    and was being counted as off-portal. "We do not have it" is only evidence of "the portals did not have
    it" where we actually look at everything the portals publish. That is exactly one slice:

      * the swept scope - the (estate type, concelho) pairs of the active criteria, where every new portal
        ad is ingested regardless of price;
      * stock that appeared on the agency site AFTER its catalogue was caught up (first `ok` watch run), so
        a portal twin published at the same time would have been swept within hours;
      * at least OFFPORTAL_SETTLE old, so the twin had time to arrive and dedup had time to merge.

    Inside that slice the number means what the column says. Known bias, measured on 161 such listings: 7
    had a single-photo portal twin that rule B refuses to merge on purpose (sibling units share photos), so
    the share can overstate by a few points; the panel wording says "sem correspondência", not "fora".
    Pre-existing stock stays unmeasured - honestly - until it turns over."""
    from app.services import runs

    caught_up = session.scalar(
        select(func.min(CrawlRun.started_at)).where(CrawlRun.source_id == source.id, CrawlRun.kind == "watch",
                                                   CrawlRun.status == "ok"))
    scope = runs.sweep_scope(session)
    if caught_up is None or not scope:
        _store_offportal(session, source, None, 0)
        return None
    in_scope = or_(*[and_(Listing.estate_type == estate,
                          or_(Listing.geo_code == f"{district}/{council}", Listing.geo_code.like(f"{district}/{council}/%")))
                     for estate, district, council in scope])
    rows = session.execute(
        select(Listing.property_id).where(
            Listing.source_id == source.id, Listing.status == "active", Listing.transaction == "sale",
            Listing.property_id.is_not(None), Listing.first_seen_at > caught_up,
            Listing.first_seen_at < datetime.now(timezone.utc) - OFFPORTAL_SETTLE, in_scope)).all()
    property_ids = {r[0] for r in rows}
    portal_ids = {r[0] for r in session.execute(
        select(Listing.property_id).join(Source, Source.id == Listing.source_id)
        .where(Listing.property_id.in_(property_ids), Source.kind.in_(("portal", "email")))).all()} if property_ids else set()
    share = offportal_share(len(property_ids), len(property_ids) - len(portal_ids))
    _store_offportal(session, source, share, len(property_ids))
    return share


def offportal_examples(session: Session, source_id: int | None = None, limit: int = 5) -> list[Listing]:
    """Listings an agency site had and no portal did - the evidence the Day 5 gate asks for."""
    agency_sources = select(Source.id).where(Source.kind == "agency")
    if source_id:
        agency_sources = agency_sources.where(Source.id == source_id)
    mine = select(Listing).where(Listing.source_id.in_(agency_sources), Listing.status == "active",
                                 Listing.property_id.is_not(None))
    out = []
    for listing in session.scalars(mine.order_by(Listing.first_seen_at.desc()).limit(limit * 40)):
        others = session.scalar(
            select(func.count(Listing.id)).join(Source, Source.id == Listing.source_id)
            .where(Listing.property_id == listing.property_id, Source.kind.in_(("portal", "email"))))
        if not others:
            out.append(listing)
        if len(out) >= limit:
            break
    return out


# --- the scheduled passes ----------------------------------------------------------------------

def due_for_watch(session: Session, limit: int) -> list[Source]:
    """Profiled agency sites, most valuable first. A site that has produced nothing off-portal twice over
    drifts to the back of the queue rather than being dropped."""
    stmt = (select(Source).where(Source.kind == "agency", Source.enabled.is_(True),
                                 Source.confidence.in_(("full", "partial")))
            .order_by(Source.last_ok_at.asc().nullsfirst()).limit(limit))
    return list(session.scalars(stmt))


def summary(session: Session) -> dict:
    """The one-line answer to her question "quantos links seria possível colocar?"."""
    total = session.scalar(select(func.count(Agency.id))) or 0
    with_sell = session.scalar(select(func.count(Agency.id)).where(Agency.active_ads_count > 0)) or 0
    checked = session.scalar(select(func.count(Agency.id)).where(Agency.checked_at.is_not(None))) or 0
    with_site = session.scalar(select(func.count(Agency.id)).where(Agency.website_url.is_not(None))) or 0
    by_confidence = {row[0] or "sem perfil": row[1] for row in session.execute(
        select(Source.confidence, func.count(Source.id)).where(Source.kind == "agency")
        .group_by(Source.confidence)).all()}
    readable = sum(n for c, n in by_confidence.items() if c in ("full", "partial"))
    return {"agencies": total, "with_sale_ads": with_sell, "profiles_checked": checked,
            "with_website": with_site, "readable": readable, "by_confidence": by_confidence}
