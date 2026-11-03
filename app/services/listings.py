"""Persist canonical listings: geo rows learned lazily, agency upsert, listing upsert with price history.
This is the only module that writes `listings`, `listing_images`, `listing_price_history`, `agencies`.
`properties` is owned by `services/properties.py` (dedup): a new listing leaves here with `property_id = NULL`
and `runs.ingest()` assigns it right after."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Agency, Geo, Listing, ListingImage, ListingPriceHistory, Source
from app.domain.listing import CanonicalListing, normalize_text, plausible_previous_price, storable_price
from app.geo.resolve import level_of


@dataclass
class PersistResult:
    listing: Listing
    is_new: bool
    price_changed: bool
    old_price: int | None
    status_changed: bool


def ensure_geo(session: Session, geo_path: list[tuple[str, str, str]]) -> None:
    """Insert any district/council/parish from a source path that the static seed does not have."""
    for code, level, name in sorted(geo_path, key=lambda p: p[0].count("/")):
        if session.get(Geo, code) is None:
            parent = code.rsplit("/", 1)[0] if "/" in code else None
            if parent and session.get(Geo, parent) is None:
                continue  # never create an orphan; the parent must come first in the path
            session.add(Geo(code=code, level=level or level_of(code), name=name, parent_code=parent))
            session.flush()


def upsert_agency(session: Session, c: CanonicalListing) -> Agency | None:
    if not c.agency_name:
        return None
    name_norm = normalize_text(c.agency_name)[:200]
    agency = session.scalar(select(Agency).where(Agency.name_norm == name_norm))
    if agency is None:
        agency = Agency(name=c.agency_name.strip()[:200], name_norm=name_norm)
        session.add(agency)
    if c.agency_slug and not agency.imovirtual_slug:
        agency.imovirtual_slug = c.agency_slug
    if c.agency_urn and not agency.source_urn:
        agency.source_urn = c.agency_urn
    if c.agency_phone and not agency.phone:
        agency.phone = c.agency_phone
    session.flush()
    return agency


def _apply(row: Listing, c: CanonicalListing, agency: Agency | None) -> None:
    row.url = c.url
    row.title = c.title
    price = storable_price(c.price)
    row.price = price
    row.currency = c.currency or "EUR"
    # A figure the column cannot hold reads to her exactly like a price the advert withheld.
    row.hide_price = c.hide_price or (c.price is not None and price is None)
    row.area_util = c.area_util
    row.area_bruta = c.area_bruta
    row.land_area = c.land_area
    row.rooms = c.rooms
    row.wc = c.wc
    row.floor = c.floor
    row.estate_type = c.estate_type
    row.transaction = c.transaction
    row.geo_code = c.geo_code
    row.street = c.street
    row.lat = c.lat
    row.lng = c.lng
    row.agency_id = agency.id if agency else None
    row.agency_name_raw = c.agency_name
    row.agency_phone_raw = c.agency_phone
    row.reference = c.reference
    row.is_private_owner = c.is_private_owner
    row.is_new_development = c.is_new_development
    row.features = c.features or {}
    row.images = c.images or []
    row.description = c.description
    row.missing_fields = c.missing_fields()
    row.status = c.status
    if c.published_at:
        row.published_at = c.published_at
    row.raw = c.raw or None
    if c.price is not None and price is None:
        # Keep the figure we refused to store. `raw` is this project's record of what the source actually
        # said, and a price dropped without a trace is unrecoverable without re-crawling the listing.
        row.raw = {**(row.raw or {}), "price_unstorable": int(c.price)}


def _sync_images(session: Session, row: Listing, urls: list[str]) -> None:
    existing = {img.position: img for img in row.images_rel}
    for position, url in enumerate(urls[:10]):
        img = existing.pop(position, None)
        if img is None:
            row.images_rel.append(ListingImage(position=position, url=url))
        elif img.url != url:
            img.url, img.phash = url, None
    for leftover in existing.values():
        session.delete(leftover)


def persist(session: Session, source: Source, c: CanonicalListing) -> PersistResult:
    ensure_geo(session, c.geo_path)
    agency = upsert_agency(session, c)
    now = datetime.now(timezone.utc)
    row = session.scalar(select(Listing).where(Listing.source_id == source.id, Listing.external_id == c.external_id))
    is_new = row is None
    old_price = None if is_new else row.price
    old_status = None if is_new else row.status
    if is_new:
        row = Listing(source_id=source.id, external_id=c.external_id, first_seen_at=now)
        session.add(row)
    _apply(row, c, agency)
    row.last_seen_at = now
    session.flush()

    # Every comparison and every history row uses the storable value, so an unstorable figure is a price we
    # do not have - never a change, and never a row that would fail the same way on insert.
    price = storable_price(c.price)
    previous_price = plausible_previous_price(storable_price(c.previous_price), price)
    price_changed = (not is_new) and price is not None and old_price is not None and price != old_price
    if is_new and previous_price and price is not None and previous_price != price:
        # A source-reported previous price is history, not an event: it happened before we ever saw the listing,
        # so the buyer is alerted once as `new` and never also as `price_drop` (RE/MAX `previousPrice` has no
        # direction - one of nine sampled listings was a price *rise*). `price_reduced_at` dates the row when
        # the source says the change was a reduction.
        session.add(ListingPriceHistory(listing_id=row.id, price=previous_price,
                                        observed_at=c.price_reduced_at or c.published_at or now, source_reported=True))
    if (is_new and price is not None) or price_changed:
        session.add(ListingPriceHistory(listing_id=row.id, price=price, observed_at=now, source_reported=False))
    if not is_new and c.status != "active" and old_status == "active":
        row.removed_at = now
    _sync_images(session, row, c.images or [])
    session.flush()
    return PersistResult(listing=row, is_new=is_new, price_changed=price_changed, old_price=old_price,
                         status_changed=(not is_new) and c.status != old_status)


def mark_removed(session: Session, row: Listing) -> None:
    if row.status == "active":
        row.status = "removed"
        row.removed_at = datetime.now(timezone.utc)
