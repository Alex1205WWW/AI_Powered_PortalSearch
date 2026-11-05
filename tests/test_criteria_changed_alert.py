"""The fourth "novo para o comprador" case: she edited the search, so a property that now qualifies is news.

The other three (`new`, `price_drop`, `data_completed`) were proven in production on 2026-11-05.
`criteria_changed` was the one wired but never tested, and it is the only one whose trigger is HER action:
she widens Rosa's price range from 650 000 to 700 000 and expects to hear about what that opened up.

Two opposite mistakes, and `rematch_criteria`'s `announce` flag is the whole line between them:
  announce=False (the default) is what initial runs, data corrections and the nightly refresh use. A
    correction that re-graded 3 000 stored properties must stay silent - that was the "300 mensagens e
    desistia do sistema no primeiro dia" outcome the proposal promised against.
  announce=True is what saving an edited criteria uses. Without it, widening a search would quietly fill
    the panel with matches she is never told about.

Runs against the real development database, in a transaction that is always rolled back. The zone is a
synthetic geo code, which keeps the scan to this test's own property instead of the thousands of real
listings in Lisbon - faster, and it cannot be perturbed by whatever was last crawled.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.db.models import (Buyer, Criteria, CriteriaZone, Geo, Listing, Notification, Property,
                           PropertyMatch, Source)
from app.services import runs


@pytest.fixture()
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


@pytest.fixture()
def world(session):
    """A buyer, a criteria in a zone of its own, and one property that qualifies."""
    tag = uuid.uuid4().hex[:8]
    district, council = f"zz{tag}", f"zz{tag}/zz{tag}"
    session.add(Geo(code=district, level="district", name="Zona de teste"))
    session.flush()
    session.add(Geo(code=council, level="council", name="Concelho de teste", parent_code=district))

    source = session.scalar(select(Source).where(Source.key == "imovirtual"))
    if source is None:
        pytest.skip("sources are not seeded in this database")

    buyer = Buyer(label=f"Teste {tag}")
    session.add(buyer)
    session.flush()

    criteria = Criteria(buyer_id=buyer.id, transaction="sale", estate_types=["apartamento"],
                        typology_min=1, typology_max=2, price_min=300_000, price_max=450_000,
                        must_have=[], nice_to_have=[], exclude=[], unknown_policy="show")
    session.add(criteria)
    session.flush()
    session.add(CriteriaZone(criteria_id=criteria.id, geo_code=council))

    prop = Property()
    session.add(prop)
    session.flush()
    session.add(Listing(source_id=source.id, external_id=f"test-{tag}", url=f"https://example.invalid/{tag}",
                        property_id=prop.id, title="Apartamento T2 de teste", price=400_000, rooms=2,
                        estate_type="apartamento", transaction="sale", geo_code=council, status="active"))
    session.flush()
    return {"buyer": buyer, "criteria": criteria, "property": prop, "source": source, "council": council}


def _alerts(session, buyer_id: int, reason: str = "criteria_changed") -> list[Notification]:
    return list(session.scalars(
        select(Notification).where(Notification.buyer_id == buyer_id, Notification.reason == reason)))


# --- the flag is the whole behaviour -------------------------------------------------------------

def test_editing_a_search_alerts_her_about_what_it_opened_up(session, world):
    matched = runs.rematch_criteria(session, world["criteria"], announce=True)
    session.flush()

    assert matched == 1
    alerts = _alerts(session, world["buyer"].id)
    assert len(alerts) == 1
    assert alerts[0].property_id == world["property"].id
    assert alerts[0].price_at_send == 400_000, "the alert must carry the price it was sent about"
    assert alerts[0].sent_at is None, "enqueued only; the flush is what delivers"


def test_a_silent_rematch_fills_the_panel_without_touching_her_phone(session, world):
    """The default. Initial runs and data corrections re-grade thousands of properties at once."""
    matched = runs.rematch_criteria(session, world["criteria"])
    session.flush()

    assert matched == 1
    assert session.scalar(select(func.count()).select_from(PropertyMatch)
                          .where(PropertyMatch.criteria_id == world["criteria"].id)) == 1
    assert _alerts(session, world["buyer"].id) == [], "a silent rematch may never alert"


def test_the_same_property_is_not_announced_twice(session, world):
    """Only a match row that did not exist before is news. The worker reruns this, she may save the form
    twice, and `matched_refresh` passes over the same properties daily."""
    runs.rematch_criteria(session, world["criteria"], announce=True)
    session.flush()
    runs.rematch_criteria(session, world["criteria"], announce=True)
    runs.rematch_criteria(session, world["criteria"], announce=True)
    session.flush()

    assert len(_alerts(session, world["buyer"].id)) == 1


def test_a_property_already_in_the_panel_is_not_announced_when_she_edits(session, world):
    """She widens the price range: the properties that already matched are not new to her, only the ones
    the edit brought in are. A silent pass first is exactly the state the panel is normally in."""
    runs.rematch_criteria(session, world["criteria"])  # already in the panel, never announced
    session.flush()

    runs.rematch_criteria(session, world["criteria"], announce=True)
    session.flush()
    assert _alerts(session, world["buyer"].id) == []


# --- and it may never announce something that does not qualify -----------------------------------

def test_a_property_outside_the_criteria_is_not_announced(session, world):
    criteria = world["criteria"]
    criteria.price_max = 350_000  # the 400 000 property no longer qualifies
    session.flush()

    runs.rematch_criteria(session, criteria, announce=True)
    session.flush()
    assert _alerts(session, world["buyer"].id) == []


def test_a_sold_listing_is_not_announced_when_she_edits_the_search(session, world):
    """She asked for this directly: "opções que já dizem Reservado, essas não é necessário que apareçam".
    An edited search must not resurrect them."""
    listing = session.scalar(select(Listing).where(Listing.property_id == world["property"].id))
    listing.status = "sold"
    session.flush()

    runs.rematch_criteria(session, world["criteria"], announce=True)
    session.flush()
    assert _alerts(session, world["buyer"].id) == []


def test_a_paused_buyer_is_not_alerted_by_an_edit(session, world):
    """Pausing is how she keeps a client quiet while she works on another. Editing that client's criteria
    must respect it - the matches stay in the panel for when she resumes."""
    world["buyer"].active = False
    session.flush()

    matched = runs.rematch_criteria(session, world["criteria"], announce=True)
    session.flush()
    assert matched == 1, "a paused buyer still has their panel kept up to date"
    assert _alerts(session, world["buyer"].id) == [], "a paused buyer gets no alert"


def test_a_criteria_with_no_zone_does_nothing_at_all(session, world):
    """An unfinished criteria (a draft she never finished) must not be read as "the whole country"."""
    session.query(CriteriaZone).filter_by(criteria_id=world["criteria"].id).delete()
    session.flush()
    session.refresh(world["criteria"])

    assert runs.rematch_criteria(session, world["criteria"], announce=True) == 0
    assert _alerts(session, world["buyer"].id) == []


def test_the_alert_reason_is_one_the_formatter_knows(session, world):
    """An unknown reason reaches Telegram as a missing heading, and `pending()` filters on this tuple -
    a typo here would silently drop the alert from the queue instead of failing."""
    from app.notify import formats
    from app.services import notifications

    runs.rematch_criteria(session, world["criteria"], announce=True)
    session.flush()
    reason = _alerts(session, world["buyer"].id)[0].reason
    assert reason in notifications.ALERT_REASONS
    assert reason in formats.HEADERS
