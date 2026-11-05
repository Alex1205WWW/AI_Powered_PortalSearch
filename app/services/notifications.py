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

# Tries before an alert is given up on. The flush runs every 2 minutes, so 30 is about an hour of a
# transient outage absorbed silently, which is the behaviour we want for a dropped connection or a
# Telegram blip. Past that it is not transient - the bot was blocked, or the chat id is wrong - and
# retrying for days while saying nothing is how 738 failures accumulated unnoticed in 24 hours.
# A given-up row keeps everything it knows (`last_error`, `attempts`) and leaves the pending queue, so it
# can neither hide the problem nor block the alerts queued behind it.
MAX_ATTEMPTS = 30


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
    """Alerts still waiting to go out. Rows given up on are excluded: the queue is ordered by
    `created_at`, so leaving a permanently undeliverable row in it would block every alert behind it."""
    stmt = (select(Notification)
            .where(Notification.sent_at.is_(None), Notification.failed_at.is_(None),
                   Notification.reason.in_(ALERT_REASONS))
            .order_by(Notification.created_at))
    return list(session.scalars(stmt))


def undelivered_count(session: Session) -> int:
    """Alerts still waiting to be sent, for the panel banner.

    Counts every pending row, not only the ones already tried. `flush()` stops the whole batch on the first
    failure, so with a dead channel exactly one group carries attempts while everything queued behind it is
    equally stuck - counting attempts reported "1 à espera" while 197 alerts were going nowhere.

    Rows with `failed_at` are excluded, because the banner says "à espera de envio" and that is no longer
    true of them: they were either given up on after `MAX_ATTEMPTS` or retired deliberately (the 197
    development-period alerts retired at handover on 2026-11-04, which were already visible in the panel
    and would have arrived as 197 messages in one burst). Leaving them in made the banner claim they were
    still waiting, for ever. The channel's own `degraded` status is what tells her delivery is broken; this
    number only says how much is queued behind it.
    """
    from sqlalchemy import func

    return int(session.scalar(
        select(func.count(Notification.id)).where(
            Notification.sent_at.is_(None),
            Notification.failed_at.is_(None),
            Notification.reason.in_(ALERT_REASONS),
        )) or 0)


def _channel(session: Session):
    """The source row standing for the Telegram channel, or None if it has not been seeded."""
    from app.services import health

    return session.scalar(select(Source).where(Source.key == health.CHANNEL_KEY))


def _record_delivery_failure(session: Session, group: list[Notification], error: str) -> None:
    """Count the attempt on every row in the group, give up past the cap, and degrade the channel.

    Called instead of letting the rows sit untouched, which is what made a dead channel invisible."""
    from app.services import health

    now = datetime.now(timezone.utc)
    for row in group:
        row.attempts = (row.attempts or 0) + 1
        row.last_error = error[:2000]
        if row.attempts >= MAX_ATTEMPTS:
            row.failed_at = now
            log.error("alert given up after %d attempts", row.attempts,
                      extra={"notification": row.id, "buyer": row.buyer_id})
    channel = _channel(session)
    if channel is not None:
        health.record_failure(channel, error)
    session.flush()


def _record_delivery_ok(session: Session) -> None:
    from app.services import health

    channel = _channel(session)
    if channel is not None:
        health.mark_ok(channel)


# The reason stored on the channel while she has not pressed Start. The banner matches on it to give her
# the one instruction that fixes it, instead of the generic "delivery is broken" wording.
NO_CHAT_REASON = "no-chat-id"

# The reason stored on the channel when the bot has no token at all. Without it the bot container idles
# quietly and reads "Up" while nothing can ever be delivered - the one failure mode that looks healthiest
# from the outside. `app/bot/main.py` is the only writer; the banner matches on it the same way.
NO_TOKEN_REASON = "no-bot-token"


def mark_channel_token_missing(session: Session) -> None:
    """The bot started with no `TELEGRAM_BOT_TOKEN`: say so where someone will see it.

    Deliberately not a delivery failure: no alert was attempted, so nothing may consume a row's attempts.
    Only the channel state moves, exactly as `_mark_channel_waiting` does for the missing chat id.
    """
    channel = _channel(session)
    if channel is None:
        return
    channel.last_error = NO_TOKEN_REASON
    if channel.status == "ok":
        channel.status = "degraded"
    log.error("telegram channel unusable: TELEGRAM_BOT_TOKEN is not set")


def clear_channel_token_missing(session: Session) -> None:
    """A token is configured now, so the stored "no token" fault no longer has a cause.

    Only ever clears the fault this module wrote. A channel degraded by a real delivery failure is left
    alone - that one is cleared by a send that succeeds, not by a bot restart.
    """
    channel = _channel(session)
    if channel is None or (channel.last_error or "") != NO_TOKEN_REASON:
        return
    channel.status = "ok"
    channel.last_error = None
    channel.consecutive_failures = 0
    log.info("telegram channel: token configured, cleared the stored no-token fault")


def _mark_channel_waiting(session: Session) -> None:
    """Show that alerts are waiting for her to connect, without consuming any row's attempts."""
    from app.services import health

    channel = _channel(session)
    if channel is None:
        return
    channel.last_error = NO_CHAT_REASON
    if channel.status == "ok":
        channel.status = "degraded"
        log.warning("telegram channel waiting: she has not pressed Start")


def record_send_result(session: Session, *, ok: bool, error: str = "") -> None:
    """Record a Telegram outcome on the channel health, for senders that are not the alert flush.

    The flush is not the only thing that talks to Telegram: the daily digest does, every morning. Without
    this, a digest that cannot be delivered left the channel reporting `ok` and the panel silent, which
    defeats the whole point of making delivery observable. There is no notification row to count here, so
    only the channel state moves.

    `services/health.py` keeps its own try/except for source alerts, and `services/budget.py` for the
    once-a-month spend warning: those are deliberately fire-and-forget, because telling her must never
    break the crawl or the AI call that triggered them."""
    if ok:
        _record_delivery_ok(session)
        return
    from app.services import health

    channel = _channel(session)
    if channel is not None:
        health.record_failure(channel, error or "envio falhou")


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
    # Resolved once per flush, from the database. None means she has not pressed Start yet.
    #
    # Held, exactly like being outside her alert hours: the rows are NOT touched and NOT counted as
    # attempts. Waiting for a precondition is not a delivery failure, and charging attempts for it would
    # retire every alert after 30 flushes - an hour - which is the same data loss by a slower route.
    # The channel state is still moved, so the panel says why nothing is arriving.
    chat_id = appsettings.chat_id(session)
    if not chat_id:
        log.info("no chat id yet: %d alert(s) held until she presses Start", len(rows))
        _mark_channel_waiting(session)
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
            # The chat id MUST come from `appsettings`, which reads the database first. `send_message`
            # alone falls back to `settings.telegram_chat_id`, the `.env` value, and that is empty by
            # design so her first `/start` can be adopted. Omitting it here meant buyer alerts would have
            # kept going nowhere even AFTER she connected, while the digest - which passes it - worked.
            message_id = telegram.send_message(text, chat_id=chat_id, reply_markup=keyboard)
        except telegram.TelegramError as exc:
            # Stop the batch (the channel is down for all of it), but record the attempt first so the
            # failure is counted, bounded, and visible in the panel instead of only in the container log.
            log.error("telegram send failed, will retry: %s", exc)
            _record_delivery_failure(session, group, str(exc))
            break
        now = datetime.now(timezone.utc)
        for row in group:
            row.sent_at = now
            row.telegram_message_id = message_id
        session.flush()
        sent += 1
    if sent:
        _record_delivery_ok(session)
    return sent
