"""Operator settings with their defaults in one place.

`runs` already reads and writes `app_settings`, but `notifications` cannot import `runs` (`runs` imports
`notifications`), so the shared helpers live here. Day 7's settings page writes through the same functions.

The defaults are the ones she agreed to on 2026-10-29: alerts 08:00-21:00, daily digest at 08:30, Europe/Lisbon.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AppSetting

ALERT_HOURS = "alert_hours"
DIGEST_AT = "digest_at"
TELEGRAM_CHAT_ID = "telegram_chat_id"
# A short message from Alex, shown at the top of every page. She has no Telegram connected
# yet, so the panel is the only place anything from him can reach her.
OPERATOR_NOTICE = "operator_notice"
ACCEPTANCE_TEST = "acceptance_test"

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


def notice(session: Session) -> dict | None:
    """The operator's message for her, or None when there is none.

    Stored as {text, url, updated_at}. `updated_at` is what lets the panel re-show a notice she has already
    dismissed once it is edited: the dismissal is remembered against that timestamp, so a NEW message is
    never hidden by an old dismissal.
    """
    raw = get(session, OPERATOR_NOTICE, None)
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    return {"text": text,
            "url": str(raw.get("url") or "").strip(),
            "updated_at": str(raw.get("updated_at") or "")}


NOTICE_MAX_CHARS = 1200


def set_notice(session: Session, text: str, url: str = "") -> tuple[bool, bool]:
    """-> (stored, truncated). Blank text removes the notice.

    `truncated` exists so the save can SAY the message was shortened. It sits above every page, so there
    is a cap - but cutting someone's last sentence off without a word is how you end up with a notice
    that stops mid-"
    """
    cleaned = (text or "").strip()
    if not cleaned:
        put(session, OPERATOR_NOTICE, None)
        return False, False
    truncated = len(cleaned) > NOTICE_MAX_CHARS
    body = cleaned[:NOTICE_MAX_CHARS]

    # `updated_at` identifies the MESSAGE, not the save. The settings form posts every field together, so
    # changing her alert hours re-submits the unchanged notice - and bumping the stamp there would
    # resurrect a notice she had already dismissed, every time she touched an unrelated setting. Keep the
    # old stamp when the words have not changed.
    link = (url or "").strip()
    if link and not link.startswith(("http://", "https://")):
        link = ""   # never render an attacker-controlled scheme; a bad link is worse than none

    existing = notice(session)
    same = existing and existing["text"] == body and existing.get("url", "") == link
    if same and existing["updated_at"]:
        stamp = existing["updated_at"]
    else:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    put(session, OPERATOR_NOTICE, {"text": body, "url": link, "updated_at": stamp})
    return True, truncated


DEFAULT_TEST_DAYS = 7


def test_window(session: Session) -> tuple[date, date] | None:
    """-> (first day, last day) of the acceptance window, both inclusive, or None before it is set.

    The contract counts only properties published inside the window, so without this the scoreboard
    silently scores the system against inventory that existed before it was ever switched on.
    """
    raw = get(session, ACCEPTANCE_TEST, None)
    if not isinstance(raw, dict) or not raw.get("start"):
        return None
    try:
        start = date.fromisoformat(str(raw["start"]))
    except ValueError:
        return None
    days = int(raw.get("days") or DEFAULT_TEST_DAYS)
    return start, start + timedelta(days=max(days, 1) - 1)


def set_test_window(session: Session, start: date, days: int = DEFAULT_TEST_DAYS) -> tuple[date, date]:
    put(session, ACCEPTANCE_TEST, {"start": start.isoformat(), "days": int(days)})
    return start, start + timedelta(days=max(int(days), 1) - 1)


def in_test_window(session: Session, moment: datetime | None) -> bool | None:
    """-> True/False inside or outside the window, None when it cannot be decided.

    None is not a soft False: a listing we never held has no publication date, and guessing either way
    moves the acceptance number. It is reported as pending so a human settles it.
    """
    window = test_window(session)
    if window is None or moment is None:
        return None
    day = moment.astimezone(tz()).date()
    return window[0] <= day <= window[1]
