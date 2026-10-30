"""Operator settings with their defaults in one place.

`runs` already reads and writes `app_settings`, but `notifications` cannot import `runs` (`runs` imports
`notifications`), so the shared helpers live here. Day 7's settings page writes through the same functions.

The defaults are the ones she agreed to on 2026-10-29: alerts 08:00-21:00, daily digest at 08:30, Europe/Lisbon.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AppSetting

ALERT_HOURS = "alert_hours"
DIGEST_AT = "digest_at"
TELEGRAM_CHAT_ID = "telegram_chat_id"

DEFAULT_ALERT_HOURS = {"from": 8, "to": 21}
DEFAULT_DIGEST_AT = "08:30"


def get(session: Session, key: str, default=None):
    row = session.get(AppSetting, key)
    return row.value if row is not None else default


def put(session: Session, key: str, value) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def alert_hours(session: Session) -> tuple[int, int]:
    raw = get(session, ALERT_HOURS, DEFAULT_ALERT_HOURS) or DEFAULT_ALERT_HOURS
    try:
        start, end = int(raw["from"]), int(raw["to"])
    except (KeyError, TypeError, ValueError):
        start, end = DEFAULT_ALERT_HOURS["from"], DEFAULT_ALERT_HOURS["to"]
    return max(0, min(23, start)), max(1, min(24, end))


def within_alert_hours(session: Session, now: datetime | None = None) -> bool:
    """Is it a civil hour at which she wants her phone to buzz?

    Outside the window nothing is sent and nothing is lost: the notification rows stay `sent_at NULL` and
    the next flush inside the window delivers them. That is why the quiet hours cannot silently swallow a
    new listing found at 02:00."""
    start, end = alert_hours(session)
    local = (now or datetime.now(tz())).astimezone(tz())
    return start <= local.hour < end


def digest_at(session: Session) -> time:
    raw = str(get(session, DIGEST_AT, DEFAULT_DIGEST_AT) or DEFAULT_DIGEST_AT)
    try:
        hour, _, minute = raw.partition(":")
        return time(int(hour), int(minute or 0))
    except ValueError:
        return time(8, 30)


def chat_id(session: Session) -> str | None:
    """Her chat id, preferring the database so it can be set without a redeploy.

    Telegram forbids a bot from opening a conversation, so this stays empty until she presses Start and the
    bot records it."""
    return str(get(session, TELEGRAM_CHAT_ID, "") or "") or (settings.telegram_chat_id or None)
