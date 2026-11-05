"""Price history must be written consistently - once per real change, never per crawl.

QA checklist item 2.4. The price-change *rules* were already tested (`plausible_previous_price`,
`storable_price`, the `price_drop` event), but nothing tested the write path, and that is where the
consequences are: deliverable 13 ("deteção de alterações de preço") is only as good as the rows
`services/listings.persist` leaves behind.

Two failure modes this pins, both silent:
  a row per crawl       -> every sweep appends the same price, so the history that shows her a drop
                           becomes thousands of identical rows and `/properties` price columns lie
  no row at all         -> a real drop has nothing to compare against, so she is never alerted

Runs against the real development database, because this is a persistence test: the FK to `listings`,
the NOT NULL on `price`, and the server-side `observed_at` default are the things being checked. Every
test rolls back, so her crawled data is never touched. Skipped, not failed, when Postgres is not up.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import ListingPriceHistory, Source
from app.domain.listing import PRICE_MAX, CanonicalListing
from app.services import listings as listing_svc


@pytest.fixture()
def session():
    """One transaction per test, always rolled back: the dev database holds real crawled listings."""
    from app.db.session import SessionLocal

    try:
        s = SessionLocal()
        s.execute(select(1))
    except Exception as exc:  # noqa: BLE001 - no database here is a skip, not a failure
        pytest.skip(f"development database unavailable: {exc}")
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture()
def source(session):
    row = session.scalar(select(Source).where(Source.key == "imovirtual"))
    if row is None:
        pytest.skip("sources are not seeded in this database")
    return row


def _listing(**over) -> CanonicalListing:
    """A canonical listing with an id nothing else can collide with."""
    fields = dict(source_key="imovirtual", external_id=f"test-{uuid.uuid4().hex[:12]}",
                  url="https://example.invalid/anuncio/teste", title="Apartamento T2 para teste",
                  price=400_000, rooms=2, estate_type="apartamento", transaction="sale")
    fields.update(over)
    return CanonicalListing(**fields)


def _history(session, listing_id: int) -> list[ListingPriceHistory]:
    return list(session.scalars(
        select(ListingPriceHistory).where(ListingPriceHistory.listing_id == listing_id)
        .order_by(ListingPriceHistory.id)))


# --- the first sighting -------------------------------------------------------------------------

def test_a_new_listing_records_its_price_once(session, source):
    result = listing_svc.persist(session, source, _listing(price=437_500))
    rows = _history(session, result.listing.id)
    assert [(r.price, r.source_reported) for r in rows] == [(437_500, False)]
    assert rows[0].observed_at is not None, "a price with no date cannot be put on a timeline"


def test_a_listing_with_no_price_records_no_history(session, source):
    """`hide_price` listings and agency sites with the price only in free text are common. A history row
    is NOT NULL on price, so inventing a zero here would read as a 0 EUR property."""
    result = listing_svc.persist(session, source, _listing(price=None))
    assert _history(session, result.listing.id) == []


def test_a_price_the_column_cannot_hold_is_not_stored_as_history_either(session, source):
    """Measured on production: feeds do publish 830 000 000 for an 830 000 EUR flat. The listing keeps no
    price, so the history must keep none - otherwise the row that fails on insert is the history row."""
    result = listing_svc.persist(session, source, _listing(price=PRICE_MAX + 1))
    assert result.listing.price is None
    assert _history(session, result.listing.id) == []


# --- what the source itself reports as the previous price ----------------------------------------

def test_a_reported_previous_price_is_stored_as_history_and_flagged(session, source):
    """RE/MAX and eGO publish the old price. It happened before we ever saw the advert, so it is history
    (`source_reported=True`) and never an event - she is alerted once as `new`, not also as `price_drop`."""
    reduced_at = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    result = listing_svc.persist(session, source, _listing(
        price=400_000, previous_price=450_000, price_reduced_at=reduced_at))
    rows = _history(session, result.listing.id)
    assert [(r.price, r.source_reported) for r in rows] == [(450_000, True), (400_000, False)]
    assert rows[0].observed_at == reduced_at, "the old price must be dated when the source says it changed"
    assert result.price_changed is False, "a first sighting is not a change we observed"


def test_a_scale_error_in_the_feed_is_not_stored_as_a_price_cut(session, source):
    """830 000 -> 830 000 000 is a feed bug, not a market move. Stored, it would show her a 99.9 % drop."""
    result = listing_svc.persist(session, source, _listing(price=830_000, previous_price=830_000_000))
    assert [(r.price, r.source_reported) for r in _history(session, result.listing.id)] == [(830_000, False)]


def test_a_reported_previous_price_equal_to_the_current_one_adds_nothing(session, source):
    result = listing_svc.persist(session, source, _listing(price=400_000, previous_price=400_000))
    assert [(r.price, r.source_reported) for r in _history(session, result.listing.id)] == [(400_000, False)]


# --- the same advert, crawled again --------------------------------------------------------------

def test_recrawling_an_unchanged_listing_does_not_grow_its_history(session, source):
    """The whole point. Imovirtual is swept every 3 h and RE/MAX every 6 h: a row per sighting would be
    thousands of identical prices per listing and would make the history unreadable."""
    canonical = _listing(price=400_000)
    first = listing_svc.persist(session, source, canonical)
    for _ in range(3):
        again = listing_svc.persist(session, source, canonical)
        assert again.listing.id == first.listing.id, "the same source + external id must be one listing"
        assert again.price_changed is False
    assert len(_history(session, first.listing.id)) == 1


def test_a_real_price_change_appends_exactly_one_row(session, source):
    canonical = _listing(price=400_000)
    first = listing_svc.persist(session, source, canonical)
    changed = listing_svc.persist(session, source, _listing(
        external_id=canonical.external_id, price=375_000))

    assert changed.price_changed is True
    assert changed.old_price == 400_000
    assert [(r.price, r.source_reported) for r in _history(session, first.listing.id)] == [
        (400_000, False), (375_000, False)]


def test_a_price_disappearing_is_not_recorded_as_a_change(session, source):
    """An advert that stops publishing its price must not be read as "price became unknown": there is
    nothing to compare and nothing to alert on."""
    canonical = _listing(price=400_000)
    first = listing_svc.persist(session, source, canonical)
    gone = listing_svc.persist(session, source, _listing(external_id=canonical.external_id, price=None))

    assert gone.price_changed is False
    assert len(_history(session, first.listing.id)) == 1


def test_the_history_survives_a_listing_being_removed_but_not_deleted(session, source):
    """`mark_removed` keeps the row, so the price timeline of a withdrawn advert stays available - it is
    evidence of what the market did, and her Excel export reads it."""
    canonical = _listing(price=400_000)
    result = listing_svc.persist(session, source, canonical)
    listing_svc.mark_removed(session, result.listing)
    session.flush()
    assert result.listing.status == "removed"
    assert len(_history(session, result.listing.id)) == 1
