"""Criteria create / update from structured input (panel form today; Telegram drafts from Day 6)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Buyer, Criteria, CriteriaZone, Geo
from app.domain.criteria import CriteriaInput


class CriteriaError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _check(session: Session, data: CriteriaInput) -> None:
    errors = data.validate()
    if session.get(Buyer, data.buyer_id) is None:
        errors.append("Comprador não encontrado.")
    if data.zones:
        known = set(session.scalars(select(Geo.code).where(Geo.code.in_(data.zones))))
        missing = [z for z in data.zones if z not in known]
        if missing:
            errors.append("Zonas desconhecidas: " + ", ".join(missing))
    if errors:
        raise CriteriaError(errors)


def _apply(criteria: Criteria, data: CriteriaInput) -> None:
    criteria.buyer_id = data.buyer_id
    criteria.transaction = data.transaction
    criteria.estate_types = list(dict.fromkeys(data.estate_types))
    criteria.typology_min = data.typology_min
    criteria.typology_max = data.typology_max
    criteria.price_min = data.price_min
    criteria.price_max = data.price_max
    criteria.area_min = data.area_min
    criteria.must_have = list(dict.fromkeys(data.must_have))
    criteria.nice_to_have = list(dict.fromkeys(data.nice_to_have))
    criteria.exclude = [e.strip() for e in data.exclude if e.strip()]
    criteria.include_private_owner = data.include_private_owner
    criteria.include_new_developments = data.include_new_developments
    criteria.unknown_policy = data.unknown_policy
    criteria.source_text = (data.source_text or "").strip() or None
    criteria.input_channel = data.input_channel
    wanted = list(dict.fromkeys(data.zones))
    criteria.zones = [CriteriaZone(geo_code=code) for code in wanted]


def create(session: Session, data: CriteriaInput, *, queue_initial_run: bool = True) -> Criteria:
    _check(session, data)
    criteria = Criteria()
    _apply(criteria, data)
    session.add(criteria)
    session.flush()
    if queue_initial_run:
        from app.services import runs  # local import: runs imports this module

        runs.queue_initial(session, criteria)
    return criteria


def update(session: Session, criteria_id: int, data: CriteriaInput) -> Criteria:
    criteria = get(session, criteria_id)
    if criteria is None:
        raise CriteriaError(["Pesquisa não encontrada."])
    _check(session, data)
    _apply(criteria, data)
    session.flush()
    # The fourth "novo" case: she changed the search, so anything already stored that newly qualifies is
    # news. Re-matching here (rather than waiting for the next sweep) is what makes an edit feel immediate.
    from app.services import runs

    runs.rematch_criteria(session, criteria, announce=True)
    return criteria


def get(session: Session, criteria_id: int) -> Criteria | None:
    return session.get(Criteria, criteria_id, options=[selectinload(Criteria.zones).selectinload(CriteriaZone.geo), selectinload(Criteria.buyer)])


def set_active(session: Session, criteria_id: int, active: bool) -> Criteria:
    criteria = session.get(Criteria, criteria_id)
    if criteria is None:
        raise CriteriaError(["Pesquisa não encontrada."])
    criteria.active = active
    return criteria


def list_active(session: Session) -> list[Criteria]:
    stmt = (
        select(Criteria)
        .join(Buyer)
        .where(Criteria.active.is_(True), Buyer.active.is_(True))
        .options(selectinload(Criteria.zones), selectinload(Criteria.buyer))
        .order_by(Criteria.id)
    )
    return list(session.scalars(stmt))


def to_input(criteria: Criteria) -> CriteriaInput:
    return CriteriaInput(
        buyer_id=criteria.buyer_id,
        transaction=criteria.transaction,
        estate_types=list(criteria.estate_types),
        typology_min=criteria.typology_min,
        typology_max=criteria.typology_max,
        price_min=criteria.price_min,
        price_max=criteria.price_max,
        area_min=criteria.area_min,
        must_have=list(criteria.must_have),
        nice_to_have=list(criteria.nice_to_have),
        exclude=list(criteria.exclude),
        zones=[z.geo_code for z in criteria.zones],
        include_private_owner=criteria.include_private_owner,
        include_new_developments=criteria.include_new_developments,
        unknown_policy=criteria.unknown_policy,
        source_text=criteria.source_text,
        input_channel=criteria.input_channel,
    )
