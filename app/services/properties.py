"""Property clustering: the only module that writes `properties`, `property_splits` and `listing_images.phash`.

assign()   decide which property a freshly persisted listing belongs to (rules in `domain.dedup`)
split()    operator says "não é o mesmo imóvel": move a listing to its own property and remember the pair
view_for() the merged view of a property that matching evaluates (best-known value per field across its listings)
views_for() the same for many properties in one query - what the rematch loop uses

Image hashes are computed only when the photo rule can apply (a candidate from another source exists), only for the
first `MAX_IMAGES` photos, cached by URL, and never stored as bytes (Milestone1_Plan.md §4.2)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Listing, ListingImage, Property, PropertySplit, Source
from app.domain import dedup
from app.domain.listing import TYPED_TIERS, normalize_text, plausible_phone
from app.sources.http import get_client

log = logging.getLogger(__name__)

MAX_CANDIDATES = 60  # closest candidates by price, then area; bounds the number of photo downloads per listing
IMAGE_DELAY_S = 0.5  # image CDNs, not the portal itself
IMAGE_MAX_BYTES = 3_000_000
# Attempts before a photo URL is given up on. `_fetch_hash` already retries once per call, and the warmer
# runs every 15 minutes, so 3 spans roughly half an hour of transient trouble before we accept the image is
# gone. Without a cap, 36 permanently dead URLs were re-fetched on every tick for ever, and a constant
# floor of 36 failures is exactly where a new, real breakage hides.
MAX_HASH_FAILURES = 3
CONFIDENCE_RANK = {"full": 0, "partial": 1, "none": 2}
_SOURCE_MAP_KEY = "imoveis.source_map"  # see source_map(): cached on Session.info, so one read per session


# --- candidates ---------------------------------------------------------------------------------

def _area(listing: Listing) -> float | None:
    return listing.area_util if listing.area_util is not None else listing.area_bruta


def candidates(session: Session, listing: Listing) -> list[Listing]:
    """Active listings of other properties inside the blocking key, minus manual splits. Empty when the listing
    cannot be placed in a council or has neither price nor area (nothing to block on)."""
    council = dedup.council_of(listing.geo_code)
    area, price = _area(listing), (None if listing.hide_price else listing.price)
    if council is None or (area is None and price is None):
        return []
    db_area = func.coalesce(Listing.area_util, Listing.area_bruta)
    stmt = select(Listing).options(selectinload(Listing.images_rel)).where(
        Listing.id != listing.id,
        Listing.status == "active",
        Listing.property_id.is_not(None),
        or_(Listing.geo_code == council, Listing.geo_code.like(f"{council}/%")),
    )
    if listing.estate_type:
        stmt = stmt.where(or_(Listing.estate_type.is_(None), Listing.estate_type == listing.estate_type))
    if listing.rooms is not None:
        stmt = stmt.where(or_(Listing.rooms.is_(None), Listing.rooms == listing.rooms))
    if area is not None:
        stmt = stmt.where(or_(db_area.is_(None), func.abs(db_area - area) <= dedup.AREA_TOL * func.greatest(db_area, area)))
    if price is not None:
        stmt = stmt.where(or_(Listing.price.is_(None), Listing.hide_price.is_(True),
                              func.abs(Listing.price - price) <= dedup.PRICE_TOL * func.greatest(Listing.price, price)))
    split_with = set(session.scalars(select(PropertySplit.listing_b).where(PropertySplit.listing_a == listing.id))) | set(
        session.scalars(select(PropertySplit.listing_a).where(PropertySplit.listing_b == listing.id)))
    if split_with:
        stmt = stmt.where(Listing.id.not_in(split_with))
    # Closest first: an Idealista card without an area can have hundreds of same-price T2 in Lisboa; the true cross-listing
    # sits at (almost) the same price and area, so the nearest MAX_CANDIDATES keep recall while bounding image downloads.
    order = [func.abs(Listing.price - price) if price is not None else Listing.id]
    if area is not None:
        order.append(func.abs(db_area - area).nulls_last())
    stmt = stmt.order_by(*order, Listing.id).limit(MAX_CANDIDATES)
    return list(session.scalars(stmt))


# --- image hashes --------------------------------------------------------------------------------

def _fetch_hash(url: str) -> int | None:
    try:
        response = get_client().get(url, delay_s=IMAGE_DELAY_S, retries=1)
        if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
            return None
        if len(response.content) > IMAGE_MAX_BYTES:
            return None
        return dedup.phash_of(response.content)
    except Exception as exc:  # noqa: BLE001 - a bad image must never break an ingest
        log.debug("phash failed for %s: %s", url, exc)
        return None


def ensure_hashes(session: Session, listing: Listing, limit: int = dedup.MAX_IMAGES) -> list[int]:
    """Unsigned pHashes of the first `limit` images, computing and caching the missing ones (by URL first)."""
    hashes: list[int] = []
    for image in sorted(listing.images_rel, key=lambda i: i.position)[:limit]:
        if image.phash is None:
            cached = session.scalar(select(ListingImage.phash).where(ListingImage.url == image.url, ListingImage.phash.is_not(None)).limit(1))
            if cached is not None:
                value = dedup.to_unsigned(cached)
            elif (image.phash_failures or 0) >= MAX_HASH_FAILURES:
                continue  # already given up on this URL; see MAX_HASH_FAILURES
            else:
                value = _fetch_hash(image.url)
                if value is None:
                    image.phash_failures = (image.phash_failures or 0) + 1
            if value is None:
                continue
            image.phash = dedup.to_signed(value)
        hashes.append(dedup.to_unsigned(image.phash))
    session.flush()
    return hashes


def side_of(listing: Listing, hashes: list[int] | None = None) -> dedup.Side:
    return dedup.Side(
        listing_id=listing.id, property_id=listing.property_id, source_id=listing.source_id,
        council=dedup.council_of(listing.geo_code), estate_type=listing.estate_type, rooms=listing.rooms,
        area=_area(listing), price=listing.price, hide_price=listing.hide_price, floor=listing.floor,
        agency_norm=normalize_text(listing.agency_name_raw) or None, reference=listing.reference,
        is_new_development=listing.is_new_development, hashes=list(hashes or []),
    )


def warm_hashes(session: Session, limit: int = 500) -> dict:
    """Hash up to `limit` not-yet-hashed photos, newest listings first.

    Dedup only ever compares the first `dedup.MAX_IMAGES` photos, so only those are warmed. Idempotent and
    resumable: it simply looks for `phash IS NULL`. Run it before a catalogue backfill, or let the worker
    nibble at it on a timer - either way the backfill then finds its candidates already hashed."""
    stmt = (select(ListingImage)
            .join(Listing, Listing.id == ListingImage.listing_id)
            .where(ListingImage.phash.is_(None), ListingImage.position < dedup.MAX_IMAGES,
                   Listing.status == "active", ListingImage.phash_failures < MAX_HASH_FAILURES)
            .order_by(ListingImage.listing_id.desc(), ListingImage.position)
            .limit(limit))
    done = failed = cached = 0
    for image in session.scalars(stmt):
        hit = session.scalar(select(ListingImage.phash).where(ListingImage.url == image.url,
                                                              ListingImage.phash.is_not(None)).limit(1))
        if hit is not None:
            image.phash, cached = hit, cached + 1
            continue
        value = _fetch_hash(image.url)
        if value is None:
            # Count the attempt, so a URL that is simply gone is not re-fetched every 15 minutes for ever.
            image.phash_failures = (image.phash_failures or 0) + 1
            failed += 1
            if image.phash_failures >= MAX_HASH_FAILURES:
                log.info("giving up on photo url after %d attempts: %s", image.phash_failures, image.url)
            continue
        image.phash, done = dedup.to_signed(value), done + 1
        if (done + failed) % 50 == 0:
            session.commit()
    session.commit()
    base = (select(func.count()).select_from(ListingImage)
            .join(Listing, Listing.id == ListingImage.listing_id)
            .where(ListingImage.phash.is_(None), ListingImage.position < dedup.MAX_IMAGES,
                   Listing.status == "active"))
    remaining = session.scalar(base.where(ListingImage.phash_failures < MAX_HASH_FAILURES))
    given_up = session.scalar(base.where(ListingImage.phash_failures >= MAX_HASH_FAILURES))
    return {"hashed": done, "from_cache": cached, "failed": failed,
            "remaining": remaining, "given_up": given_up}


# --- assign / split -----------------------------------------------------------------------------

def assign(session: Session, listing: Listing) -> dedup.Decision:
    """Attach a listing to an existing property or create its own. Idempotent: a listing that already has a
    property keeps it."""
    if listing.property_id is not None:
        return dedup.Decision(listing.property_id, "kept")
    cands = candidates(session, listing)
    new_side = side_of(listing)
    sides: list[dedup.Side] = []
    cross_source = [c for c in cands if c.source_id != listing.source_id]
    if cross_source and not listing.is_new_development:
        new_side.hashes = ensure_hashes(session, listing)
    for cand in cands:
        hashes = ensure_hashes(session, cand) if (cand in cross_source and new_side.hashes and not cand.is_new_development) else []
        sides.append(side_of(cand, hashes))
    decision = dedup.decide(new_side, sides)
    if decision.property_id is None:
        prop = Property()
        session.add(prop)
        session.flush()
        decision = dedup.Decision(prop.id, decision.rule, decision.note)
    listing.property_id = decision.property_id
    session.flush()
    if decision.rule in (dedup.RULE_REFERENCE, dedup.RULE_PHOTOS, dedup.RULE_AMBIGUOUS):
        log.info("dedup: listing %s -> property %s by %s %s", listing.id, decision.property_id, decision.rule, decision.note)
    return decision


def split(session: Session, listing_id: int) -> Property:
    """Operator override: this listing is not the same property as the others in its cluster."""
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise ValueError("Anúncio não encontrado.")
    siblings = list(session.scalars(select(Listing).where(Listing.property_id == listing.property_id, Listing.id != listing.id)))
    if not siblings:
        raise ValueError("Este anúncio já está sozinho.")
    for sib in siblings:
        a, b = sorted((listing.id, sib.id))
        if session.get(PropertySplit, (a, b)) is None:
            session.add(PropertySplit(listing_a=a, listing_b=b))
    prop = Property()
    session.add(prop)
    session.flush()
    listing.property_id = prop.id
    session.flush()
    return prop


# --- merged view ---------------------------------------------------------------------------------

@dataclass
class PropertyView:
    """Best-known value per field across the active listings of one property; duck-typed for `matching.evaluate`."""

    property_id: int
    listings: list[Listing]
    status: str = "active"
    transaction: str = "sale"
    estate_type: str | None = None
    geo_code: str | None = None
    rooms: int | None = None
    price: int | None = None
    hide_price: bool = False
    area_util: float | None = None
    area_bruta: float | None = None
    floor: int | None = None
    is_private_owner: bool = False
    is_new_development: bool = False
    title: str | None = None
    description: str | None = None
    features: dict = field(default_factory=dict)

    @property
    def primary(self) -> Listing:
        return self.listings[0]


def _rank(source_by_id: dict[int, Source], listing: Listing) -> tuple[int, datetime]:
    source = source_by_id.get(listing.source_id)
    return (CONFIDENCE_RANK.get(source.confidence if source else None, 1), listing.first_seen_at)


def build_view(property_id: int, listings: list[Listing], source_by_id: dict[int, Source]) -> PropertyView:
    active = [l for l in listings if l.status == "active"] or list(listings)
    ordered = sorted(active, key=lambda l: _rank(source_by_id, l))
    view = PropertyView(property_id=property_id, listings=ordered, status="active" if any(l.status == "active" for l in listings) else ordered[0].status)
    first = lambda attr: next((getattr(l, attr) for l in ordered if getattr(l, attr) is not None), None)
    view.transaction = ordered[0].transaction
    view.estate_type = first("estate_type")
    view.rooms = first("rooms")
    view.area_util, view.area_bruta, view.floor = first("area_util"), first("area_bruta"), first("floor")
    priced = next((l for l in ordered if l.price is not None and not l.hide_price), None)
    view.price, view.hide_price = (priced.price, False) if priced else (first("price"), True if first("price") is not None else False)
    view.geo_code = max((l.geo_code for l in ordered if l.geo_code), key=lambda c: c.count("/"), default=None)  # most specific
    view.is_private_owner = any(l.is_private_owner for l in ordered)
    view.is_new_development = any(l.is_new_development for l in ordered)
    view.title = first("title")
    view.description = " ".join(l.description for l in ordered if l.description) or None
    merged: dict[str, dict] = {}
    for listing in ordered:
        for key, entry in (listing.features or {}).items():
            current = merged.get(key)
            if current is None or (entry.get("tier") in TYPED_TIERS and current.get("tier") not in TYPED_TIERS):
                merged[key] = entry
    view.features = merged
    return view


def contact_for(primary: Listing, listings: Sequence[Listing],
                agency_by_id: dict[int, object] | None = None) -> tuple[str | None, str | None]:
    """(agency name, dialable phone) for one property, in the order she would want to be told.

    The panel and the Excel export both used the PRIMARY listing's phone and nothing else, which left a
    number she can call on only 68.9 % of her 1 515 shown properties - while the system actually held one
    for 90.4 % of them (measured 2026-11-05). The missing fifth sits on the agency record, put there by
    the Imovirtual agency directory, and nothing ever looked at it.

    Order: the primary listing, then any sibling listing of the same property, then the agency record.
    The sibling step only recovers 10 rows, but this is the one place that resolves a contact, so the
    whole chain lives here rather than being half-applied in two call sites.

    The agency fallback is safe to trust, checked rather than assumed: of the 271 shown properties where
    the listing's phone and the agency record's phone BOTH existed and disagreed, the agency NAME was
    identical in 271 of 271. They are simply two real lines for the same agency (office, second office,
    agent), so the agency record never points somewhere else. Where both exist the listing's own number
    still wins, because it is the one printed on that advert.
    """
    name = primary.agency_name_raw or next(
        (l.agency_name_raw for l in listings if l.agency_name_raw), None)
    phone = plausible_phone(primary.agency_phone_raw)
    if phone is None:
        phone = next((p for p in (plausible_phone(l.agency_phone_raw) for l in listings) if p), None)
    if phone is None and agency_by_id:
        for candidate in (primary, *listings):
            agency = agency_by_id.get(candidate.agency_id) if candidate.agency_id else None
            phone = plausible_phone(getattr(agency, "phone", None)) if agency else None
            if phone:
                break
    return name, phone


def source_map(session: Session) -> dict[int, Source]:
    """Every source by id, read once per session.

    `build_view` needs this only to rank listings by their source's `confidence`, and a session is one job,
    one request or one bot update - short enough that a source being enabled mid-session cannot matter, and
    `_rank` would only reorder equally-good listings if it did. Without the cache this was the single most
    repeated query in the system: `pg_stat_user_tables` recorded 48 966 sequential scans of `sources`,
    because a merged property re-read the whole table every time one was built."""
    cached = session.info.get(_SOURCE_MAP_KEY)
    if cached is None:
        cached = {s.id: s for s in session.scalars(select(Source))}
        session.info[_SOURCE_MAP_KEY] = cached
    return cached


def view_for(session: Session, property_id: int) -> Listing | PropertyView | None:
    """A single-listing property is evaluated as the listing itself; otherwise the merged view. None when the
    property has no listings left (only possible after a manual delete)."""
    listings = list(session.scalars(select(Listing).where(Listing.property_id == property_id).order_by(Listing.first_seen_at)))
    if not listings:
        return None
    if len(listings) == 1:
        return listings[0]
    return build_view(property_id, listings, source_map(session))


def views_for(session: Session, property_ids: Sequence[int]) -> dict[int, Listing | PropertyView]:
    """`view_for` for many properties in ONE query instead of one per property.

    `rematch_criteria` evaluates every property in a criteria's zones, so the per-property query was an N+1
    that grew with her catalogue and with every buyer added: 33 131 properties are in scope today and the
    contract is 50 buyers. Properties with no listings left are simply absent from the result, which is the
    same thing `view_for` says with None."""
    ids = list(property_ids)
    if not ids:
        return {}
    grouped: dict[int, list[Listing]] = {}
    for listing in session.scalars(
        select(Listing).where(Listing.property_id.in_(ids)).order_by(Listing.property_id, Listing.first_seen_at)
    ):
        grouped.setdefault(listing.property_id, []).append(listing)
    sources = source_map(session) if any(len(v) > 1 for v in grouped.values()) else {}
    return {pid: (listings[0] if len(listings) == 1 else build_view(pid, listings, sources))
            for pid, listings in grouped.items()}
