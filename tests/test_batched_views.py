"""`views_for` must be `view_for`, in bulk.

The rematch loop used to call `view_for` once per property, which is an N+1 that grows with the catalogue
and with every buyer added. `views_for` replaces it with one query per chunk, so the only thing that matters
is that it returns the *same subject* for every property - a merged PropertyView where `view_for` merges, the
Listing itself where `view_for` short-circuits, and nothing at all where `view_for` says None.

Run against the real development database, because the interesting properties are the ones with several
listings from different sources, and those exist in real crawled data rather than in a fixture. Skipped
rather than failed when Postgres is not up, so the suite still runs without it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db.models import Listing
from app.services import properties as prop_svc


@pytest.fixture(scope="module")
def session():
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


def _sample(session, limit: int = 300) -> list[int]:
    """Property ids worth comparing: every multi-listing property first, then single-listing ones."""
    multi = list(session.scalars(
        select(Listing.property_id).where(Listing.property_id.is_not(None))
        .group_by(Listing.property_id).having(func.count() > 1).limit(limit)))
    single = list(session.scalars(
        select(Listing.property_id).where(Listing.property_id.is_not(None))
        .group_by(Listing.property_id).having(func.count() == 1).limit(limit)))
    return multi + single


def _fingerprint(subject):
    """The fields matching actually reads, so an equal fingerprint means an equal decision."""
    if subject is None:
        return None
    return (
        type(subject).__name__,
        getattr(subject, "property_id", None), subject.status, subject.transaction,
        subject.estate_type, subject.geo_code, subject.rooms, subject.price, subject.hide_price,
        subject.area_util, subject.area_bruta, subject.floor,
        subject.is_private_owner, subject.is_new_development, subject.title,
        tuple(sorted((k, v.get("tier")) for k, v in (subject.features or {}).items())),
    )


def test_views_for_returns_exactly_what_view_for_returns(session):
    ids = _sample(session)
    if not ids:
        pytest.skip("development database has no clustered properties yet")
    batched = prop_svc.views_for(session, ids)
    mismatched = []
    for pid in ids:
        one = prop_svc.view_for(session, pid)
        many = batched.get(pid)
        if _fingerprint(one) != _fingerprint(many):
            mismatched.append(pid)
    assert not mismatched, f"views_for disagreed with view_for on properties {mismatched[:10]}"
    # A property view_for can build must be present in the batch, and nothing extra may appear.
    assert set(batched) <= set(ids)
    assert {pid for pid in ids if prop_svc.view_for(session, pid) is not None} == set(batched)


def test_views_for_merges_multi_listing_properties_not_just_the_first(session):
    """Guards the case the single-listing short-circuit could hide: with >1 listing the answer must be a
    merged view, not one of the listings."""
    multi = list(session.scalars(
        select(Listing.property_id).where(Listing.property_id.is_not(None))
        .group_by(Listing.property_id).having(func.count() > 1).limit(25)))
    if not multi:
        pytest.skip("development database has no multi-listing property yet")
    batched = prop_svc.views_for(session, multi)
    assert batched, "multi-listing properties produced no views"
    for pid in multi:
        assert isinstance(batched[pid], prop_svc.PropertyView), f"property {pid} was not merged"


def test_views_for_is_one_query_per_chunk_not_one_per_property(session):
    """The whole point. Counts statements instead of timing, so it cannot flake on a slow machine."""
    from sqlalchemy import event

    ids = _sample(session, limit=60)
    if len(ids) < 2:
        pytest.skip("development database has too few properties to compare")
    prop_svc.source_map(session)  # warm the cached source read, which is not per-property either way
    counter = {"n": 0}

    def count(*_a, **_k):
        counter["n"] += 1

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", count)
    try:
        counter["n"] = 0
        prop_svc.views_for(session, ids)
        batched = counter["n"]
        counter["n"] = 0
        for pid in ids:
            prop_svc.view_for(session, pid)
        per_property = counter["n"]
    finally:
        event.remove(bind, "before_cursor_execute", count)

    assert batched <= 2, f"views_for issued {batched} statements for one chunk"
    assert per_property >= len(ids), "expected view_for to cost at least one statement per property"
    assert batched < per_property


def test_source_map_is_read_once_per_session(session):
    from sqlalchemy import event

    counter = {"n": 0}

    def count(*_a, **_k):
        counter["n"] += 1

    session.info.pop("imoveis.source_map", None)
    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", count)
    try:
        first = prop_svc.source_map(session)
        after_first = counter["n"]
        again = prop_svc.source_map(session)
        after_second = counter["n"]
    finally:
        event.remove(bind, "before_cursor_execute", count)

    assert again is first, "the second call rebuilt the map instead of reusing it"
    assert after_second == after_first, "the cached source map still hit the database"


# --- the preloaded match map ---------------------------------------------------------------------
# `rematch_criteria` reads every stored match for a criteria once and hands each row to
# `evaluate_and_store`, instead of letting it SELECT per property. These cover the three ways that
# argument can arrive, with a stub session, so the rules are pinned without a database.

from dataclasses import dataclass, field  # noqa: E402

from app.services import matches as match_svc  # noqa: E402


@dataclass
class _Zone:
    geo_code: str


@dataclass
class _Criteria:
    id: int = 1
    buyer_id: int = 1
    zones: list = field(default_factory=lambda: [_Zone("lisboa/sintra")])
    transaction: str = "sale"
    estate_types: list = field(default_factory=lambda: ["moradia"])
    typology_min: int | None = 2
    typology_max: int | None = None
    price_min: int | None = 400_000
    price_max: int | None = 650_000
    area_min: int | None = None
    must_have: list = field(default_factory=list)
    nice_to_have: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    include_private_owner: bool = True
    include_new_developments: bool = True
    unknown_policy: str = "show"


class _StubSession:
    """Counts the SELECTs `evaluate_and_store` makes, and remembers what it added."""

    def __init__(self, found=None):
        self.scalar_calls = 0
        self.found = found
        self.added = []

    def scalar(self, _stmt):
        self.scalar_calls += 1
        return self.found

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass


def _matching_subject():
    from app.domain.listing import CanonicalListing
    s = CanonicalListing(source_key="imovirtual", external_id="1", url="https://x/1", price=520_000,
                         rooms=3, estate_type="moradia", geo_code="lisboa/sintra/algueirao", area_util=140.0)
    s.property_id = 7
    return s


def test_a_supplied_match_is_used_without_a_query():
    from app.db.models import PropertyMatch

    row = PropertyMatch(property_id=7, criteria_id=1, buyer_id=1, grade="possivel")
    session = _StubSession()
    stored = match_svc.evaluate_and_store(session, _matching_subject(), _Criteria(), row)
    assert session.scalar_calls == 0, "a supplied match must not be looked up again"
    assert stored.match is row and stored.created is False
    assert stored.previous_grade == "possivel"
    assert session.added == [], "an existing row must be updated, never inserted"


def test_a_supplied_none_is_reconfirmed_before_inserting():
    """The map is read before the loop, and the panel and the worker both rematch without a lock between
    them, so the INSERT path must re-read rather than trust a stale absence."""
    session = _StubSession(found=None)
    stored = match_svc.evaluate_and_store(session, _matching_subject(), _Criteria(), None)
    assert session.scalar_calls == 1, "about to insert, so the absence must be confirmed once"
    assert stored.created is True and len(session.added) == 1


def test_a_supplied_none_costs_nothing_when_the_property_does_not_match():
    """The common case across a buyer's zone: no stored row, and nothing to store."""
    subject = _matching_subject()
    subject.price = 2_000_000  # above price_max
    session = _StubSession(found=None)
    stored = match_svc.evaluate_and_store(session, subject, _Criteria(), None)
    assert session.scalar_calls == 0, "no row and no match means no query at all"
    assert stored.match is None and stored.created is False and session.added == []


def test_omitting_the_argument_still_looks_the_match_up():
    session = _StubSession(found=None)
    match_svc.evaluate_and_store(session, _matching_subject(), _Criteria())
    assert session.scalar_calls == 1, "the default path must behave exactly as before"
