"""Evaluate listings (or merged property views) against criteria and store `property_matches`.
Only this module writes that table."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Criteria, PropertyMatch
from app.domain.events import GRADE_OUT
from app.domain.matching import MatchResult, evaluate


@dataclass
class StoredMatch:
    match: PropertyMatch | None
    result: MatchResult
    created: bool
    previous_grade: str | None


def zone_codes(criteria: Criteria) -> list[str]:
    return [z.geo_code for z in criteria.zones]


LOOK_IT_UP = object()  # `existing` was not supplied, so evaluate_and_store queries for the row itself


def _stored_match(session: Session, property_id: int, criteria: Criteria) -> PropertyMatch | None:
    return session.scalar(
        select(PropertyMatch).where(PropertyMatch.property_id == property_id, PropertyMatch.criteria_id == criteria.id)
    )


def existing_by_property(session: Session, criteria: Criteria) -> dict[int, PropertyMatch]:
    """Every stored match for one criteria, by property id.

    For callers that evaluate a criteria against many properties in a row: passing the row in spares
    `evaluate_and_store` its per-property SELECT, which was the last query the rematch loop still paid for
    every property in a buyer's zones."""
    return {m.property_id: m for m in session.scalars(select(PropertyMatch).where(PropertyMatch.criteria_id == criteria.id))}


def evaluate_and_store(session: Session, subject: Any, criteria: Criteria, existing: Any = LOOK_IT_UP) -> StoredMatch:
    """`subject` is a `Listing` or a `properties.PropertyView`: anything with `property_id` and the listing fields.

    `existing` is the already-loaded `PropertyMatch` for this (property, criteria), or None when the caller
    knows there is none - from `existing_by_property`. Left out, the row is looked up here as before."""
    result = evaluate(subject, criteria, zone_codes(criteria))
    supplied = existing is not LOOK_IT_UP
    if not supplied:
        existing = _stored_match(session, subject.property_id, criteria)
    elif existing is None and result.ok:
        # The caller's map was read before the loop started, and `rematch_criteria` runs both from the panel
        # (a criteria being saved) and from the worker, with no lock between them. Re-reading here, and only
        # on the path that is about to INSERT, keeps the window for a duplicate exactly as narrow as it was
        # when every property paid for this query - while the properties that match nothing, which are most
        # of a buyer's zone, now cost none at all.
        existing = _stored_match(session, subject.property_id, criteria)
    previous_grade = existing.grade if existing else None
    if result.ok:
        if existing is None:
            existing = PropertyMatch(property_id=subject.property_id, criteria_id=criteria.id, buyer_id=criteria.buyer_id, grade=result.grade)
            session.add(existing)
            created = True
        else:
            created = False
            existing.grade = result.grade
        existing.satisfied, existing.unsatisfied, existing.unknown = result.satisfied, result.unsatisfied, result.unknown
        session.flush()
        return StoredMatch(existing, result, created, previous_grade)
    if existing is not None and existing.grade != GRADE_OUT:
        existing.grade = GRADE_OUT
        existing.satisfied, existing.unsatisfied, existing.unknown = result.satisfied, result.unsatisfied, result.unknown
        session.flush()
    return StoredMatch(existing, result, False, previous_grade)


def set_state(session: Session, match_id: int, state: str, commission_share_pct=None, notes: str | None = None) -> PropertyMatch:
    from app.domain.criteria import STATES

    match = session.get(PropertyMatch, match_id)
    if match is None:
        raise ValueError("Correspondência não encontrada.")
    if state not in STATES:
        raise ValueError("Estado inválido.")
    match.state = state
    match.state_updated_at = datetime.now(timezone.utc)
    if commission_share_pct is not None:
        match.commission_share_pct = commission_share_pct
    if notes is not None:
        match.notes = notes.strip() or None
    return match
