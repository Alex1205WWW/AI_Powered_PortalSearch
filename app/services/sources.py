from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Source
from app.sources import robots
from app.sources.registry import SOURCE_SEED


def seed(session: Session) -> int:
    """Insert missing seed sources; never overwrite operator changes (enabled, schedule) on existing rows."""
    existing = set(session.scalars(select(Source.key)))
    added = 0
    for spec in SOURCE_SEED:
        if spec["key"] in existing:
            continue
        session.add(Source(**spec))
        added += 1
    session.flush()
    return added


def list_all(session: Session, kind: str | None = None) -> list[Source]:
    stmt = select(Source).order_by(Source.kind, Source.enabled.desc(), Source.name)
    if kind:
        stmt = stmt.where(Source.kind == kind)
    return list(session.scalars(stmt))


def get_by_key(session: Session, key: str) -> Source | None:
    return session.scalar(select(Source).where(Source.key == key))


def refresh_robots(session: Session) -> list[tuple[str, int | None, bool | None]]:
    return robots.refresh_all(session)


def set_enabled(session: Session, source_id: int, enabled: bool) -> Source:
    source = session.get(Source, source_id)
    if source is None:
        raise ValueError("Fonte não encontrada.")
    source.enabled = enabled
    return source
