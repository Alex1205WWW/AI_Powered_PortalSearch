"""The daily summary (deliverable 10): one message per day, per buyer, at her hour.

Deliberately separate from alerts. Alerts are "something happened"; the digest is "here is where you stand",
and it is sent even on a quiet day - a digest full of zeros is how she knows the system is alive rather than
silently broken. That is also why a degraded-agency count rides along: with hundreds of agency sources we no
longer message her about each one (it would be a flood), so the digest is where that surfaces.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Buyer, Notification, PropertyMatch, Source
from app.notify import formats, telegram
from app.services import appsettings

log = logging.getLogger(__name__)
DIGEST_REASON = "digest"
# The digest is NOT stored in `notifications`: that table requires a buyer_id and its uniqueness constraint
# treats NULLs as equal, so a buyer-less digest row would fail today and collide with tomorrow's. One
# app_settings key is the whole bookkeeping requirement.
LAST_SENT_KEY = "digest_last_sent"


def per_buyer(session: Session, since: datetime) -> list[tuple[str, int, int, int]]:
    """(label, new since `since`, confirmed total, possible total) for every active buyer.

    "New" counts the alerts actually queued for her in the window, not matches created: a match that was
    created by a silent initial run is not news, and counting it would make the digest disagree with what
    her phone showed."""
    rows: list[tuple[str, int, int, int]] = []
    for buyer in session.scalars(select(Buyer).where(Buyer.active.is_(True)).order_by(Buyer.label)):
        new_count = session.scalar(
            select(func.count(Notification.id)).where(Notification.buyer_id == buyer.id,
                                                      Notification.created_at >= since,
                                                      Notification.reason != DIGEST_REASON)) or 0
        grades = dict(session.execute(
            select(PropertyMatch.grade, func.count(PropertyMatch.id))
            .where(PropertyMatch.buyer_id == buyer.id).group_by(PropertyMatch.grade)).all())
        rows.append((buyer.label, int(new_count), int(grades.get("confirmada", 0)), int(grades.get("possivel", 0))))
    return rows


def degraded_agencies(session: Session) -> int:
    return session.scalar(
        select(func.count(Source.id)).where(Source.kind == "agency", Source.enabled.is_(True),
                                            Source.status.in_(("degraded", "blocked")))) or 0


def already_sent_today(session: Session, local_date) -> bool:
    """One digest per day, even if the job fires twice (a restart near her hour, a missed-run catch-up)."""
    return str(appsettings.get(session, LAST_SENT_KEY, "") or "") == local_date.isoformat()


def send(session: Session, *, force: bool = False) -> bool:
    """Send today's digest if it is due and not already sent. -> True when a message went out."""
    now_local = datetime.now(appsettings.tz())
    if not force and already_sent_today(session, now_local.date()):
        return False
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = per_buyer(session, since)
    text = formats.digest(now_local.strftime("%d/%m"), rows, degraded_agencies=degraded_agencies(session),
                          panel_url=panel_url())
    chat = appsettings.chat_id(session)
    if not chat:
        log.info("digest not sent: no chat id yet (she has not pressed Start)")
        return False
    # A failed digest must not kill the job, must not be marked as sent (so the next tick inside the slack
    # window retries it), and must show up on the channel health - the digest is the second delivery path
    # and used to be the one that failed invisibly.
    from app.services import notifications

    try:
        telegram.send_message(text, chat_id=chat)
    except Exception as exc:  # noqa: BLE001 - any transport failure, recorded rather than raised
        notifications.record_send_result(session, ok=False, error=str(exc))
        log.error("digest not delivered, will retry: %s", exc)
        return False
    notifications.record_send_result(session, ok=True)
    appsettings.put(session, LAST_SENT_KEY, now_local.date().isoformat())
    session.flush()
    log.info("digest sent for %d buyer(s)", len(rows))
    return True


def due_now(session: Session, *, slack_minutes: int = 20) -> bool:
    """Is the clock within the digest window? The job ticks more often than once a day so that a restart
    across her hour does not skip the day entirely."""
    target = appsettings.digest_at(session)
    now_local = datetime.now(appsettings.tz())
    minutes_now = now_local.hour * 60 + now_local.minute
    minutes_target = target.hour * 60 + target.minute
    return 0 <= minutes_now - minutes_target <= slack_minutes


def panel_url() -> str | None:
    from app.config import settings

    base = (settings.panel_base_url or "").strip().rstrip("/")
    return base or None
