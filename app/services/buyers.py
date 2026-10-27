from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Buyer, Criteria, CriteriaZone


class BuyerError(ValueError):
    pass


def create(session: Session, label: str) -> Buyer:
    label = " ".join(label.split())
    if not label:
        raise BuyerError("A etiqueta do comprador é obrigatória.")
    if len(label) > 80:
        raise BuyerError("Etiqueta demasiado longa (máx. 80).")
    if session.scalar(select(Buyer).where(func.lower(Buyer.label) == label.lower())):
        raise BuyerError(f"Já existe um comprador com a etiqueta '{label}'.")
    buyer = Buyer(label=label)
    session.add(buyer)
    session.flush()
    return buyer


def get(session: Session, buyer_id: int) -> Buyer | None:
    return session.get(
        Buyer,
        buyer_id,
        options=[selectinload(Buyer.criteria).selectinload(Criteria.zones).selectinload(CriteriaZone.geo)],
    )


def list_all(session: Session, active: bool | None = None) -> list[Buyer]:
    stmt = select(Buyer).options(selectinload(Buyer.criteria)).order_by(Buyer.active.desc(), Buyer.label)
    if active is not None:
        stmt = stmt.where(Buyer.active.is_(active))
    return list(session.scalars(stmt))


def pause(session: Session, buyer_id: int) -> Buyer:
    buyer = session.get(Buyer, buyer_id)
    if buyer is None:
        raise BuyerError("Comprador não encontrado.")
    buyer.active = False
    buyer.paused_at = datetime.now(timezone.utc)
    return buyer


def resume(session: Session, buyer_id: int) -> Buyer:
    buyer = session.get(Buyer, buyer_id)
    if buyer is None:
        raise BuyerError("Comprador não encontrado.")
    buyer.active = True
    buyer.paused_at = None
    return buyer


def delete(session: Session, buyer_id: int) -> None:
    buyer = session.get(Buyer, buyer_id)
    if buyer is not None:
        session.delete(buyer)
