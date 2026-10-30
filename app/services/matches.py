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


def evaluate_and_store(session: Session, subject: Any, criteria: Criteria) -> StoredMatch:
    """`subject` is a `Listing` or a `properties.PropertyView`: anything with `property_id` and the listing fields."""
    result = evaluate(subject, criteria, zone_codes(criteria))
    existing = session.scalar(
        select(PropertyMatch).where(PropertyMatch.property_id == subject.property_id, PropertyMatch.criteria_id == criteria.id)
    )
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
