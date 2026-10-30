"""Notification rows (DB-unique = exactly once) and the Telegram flush. Only this module writes `notifications`."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Buyer, Criteria, Geo, Listing, Notification, PropertyMatch, Source
from app.notify import formats, telegram
from app.services import appsettings

log = logging.getLogger(__name__)
ALERT_REASONS = ("new", "price_drop", "data_completed", "criteria_changed")


def enqueue(session: Session, *, buyer_id: int, property_id: int, reason: str, price: int | None, payload: dict | None = None) -> bool:
    """Insert one notification; silently ignored if the same (buyer, property, reason, price) was already queued.
    Paused buyers get nothing queued, so resuming never floods them with the paused window."""
    buyer = session.get(Buyer, buyer_id)
    if buyer is None or not buyer.active:
        return False
    stmt = (
        pg_insert(Notification)
        .values(buyer_id=buyer_id, property_id=property_id, reason=reason, price_at_send=price, payload=payload)
        .on_conflict_do_nothing(constraint="uq_notification_once")
        .returning(Notification.id)
    )
    # `rowcount` is -1 for INSERT ... ON CONFLICT under psycopg3 (verified 2026-10-28); RETURNING yields the id only on a real insert.
    return session.execute(stmt).scalar() is not None


def pending(session: Session) -> list[Notification]:
    stmt = select(Notification).where(Notification.sent_at.is_(None), Notification.reason.in_(ALERT_REASONS)).order_by(Notification.created_at)
    return list(session.scalars(stmt))


def flush(session: Session, *, ignore_hours: bool = False) -> int:
    """Send every unsent alert, one Telegram message per (property, reason) with all buyers inside.
    A Telegram failure leaves the rows unsent; the next run retries.

    Outside her alert hours nothing is sent and nothing is dropped: the rows stay pending and the next
    flush inside the window delivers them, so a listing found at 02:00 still reaches her at 08:00.
    `ignore_hours=True` is for the CLI, where she is explicitly asking for a flush now."""
    rows = pending(session)
    if not rows:
        return 0
    if not ignore_hours and not appsettings.within_alert_hours(session):
        log.info("outside alert hours: %d alert(s) held", len(rows))
        return 0
    groups: dict[tuple[int, str], list[Notification]] = defaultdict(list)
    for row in rows:
        groups[(row.property_id, row.reason)].append(row)
    source_keys = {s.id: s.key for s in session.scalars(select(Source))}
    sent = 0
    for (property_id, reason), group in groups.items():
        listings = list(session.scalars(select(Listing).where(Listing.property_id == property_id).order_by(Listing.first_seen_at)))
        if not listings:
            for row in group:
                row.sent_at = datetime.now(timezone.utc)
            continue
        primary = next((l for l in listings if l.status == "active"), listings[0])
        buyers: list[tuple[str, PropertyMatch, list[str]]] = []
        for row in group:
            match = session.scalar(
                select(PropertyMatch).where(PropertyMatch.property_id == property_id, PropertyMatch.buyer_id == row.buyer_id).order_by(PropertyMatch.id.desc())
            )
            if match is None:
                continue
            criteria = session.get(Criteria, match.criteria_id)
            buyer = session.get(Buyer, row.buyer_id)
            buyers.append((buyer.label, match, list(criteria.must_have) if criteria else []))
        zone = session.get(Geo, primary.geo_code) if primary.geo_code else None
        zone_name = None
        if zone is not None:
            parent = session.get(Geo, zone.parent_code) if zone.parent_code and zone.level == "parish" else None
            zone_name = f"{zone.name}, {parent.name}" if parent else zone.name
        old_price = (group[0].payload or {}).get("old_price") if group[0].payload else None
        text = formats.property_alert(reason, primary, listings, buyers, zone_name, source_keys, old_price=old_price)
        # One row of state buttons per buyer in this message, so she can mark it without opening the panel.
        keyboard = formats.state_keyboard([(m.id, label) for label, m, _must in buyers]) if buyers else None
        try:
            message_id = telegram.send_message(text, reply_markup=keyboard)
        except telegram.TelegramError as exc:
            log.error("telegram send failed, will retry: %s", exc)
            break
        now = datetime.now(timezone.utc)
        for row in group:
            row.sent_at = now
            row.telegram_message_id = message_id
        session.flush()
        sent += 1
    return sent
