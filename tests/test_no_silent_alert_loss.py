"""An alert may never be recorded as delivered unless Telegram really accepted it.

Found in production on 2026-11-05: **96 of her alerts were destroyed** on 2026-11-04 between 08:00 and
15:42. `telegram.send_message` logged a warning and returned `None` when no chat id was configured;
`notifications.flush()` cannot tell that from a successful send, so it stamped `sent_at` on all of them.
Marked delivered, never sent, and gone from the pending queue for good. The channel health even stayed
`ok`, so the panel reported everything as fine.

The window was opened by clearing `TELEGRAM_CHAT_ID` during the handover so her first `/start` could be
adopted - a deliberate change that converted a safe "queue until it works" state into silent data loss.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.notify import telegram


# --- the transport must never signal failure by a return value ----------------------------------

def test_no_chat_id_raises_instead_of_returning_none(monkeypatch):
    monkeypatch.setattr(telegram.settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(telegram.settings, "telegram_chat_id", "")
    with pytest.raises(telegram.TelegramError) as caught:
        telegram.send_message("olá")
    assert "no chat id" in str(caught.value).lower()


def test_no_token_raises_instead_of_returning_empty(monkeypatch):
    monkeypatch.setattr(telegram.settings, "telegram_bot_token", "")
    with pytest.raises(telegram.TelegramError):
        telegram.send_message("olá", chat_id="123")


def test_send_message_never_returns_none_on_a_failure_path():
    """Structural: the only honest outcomes are a message id or an exception."""
    import inspect

    source = inspect.getsource(telegram.send_message)
    body = source.replace(telegram.send_message.__doc__ or "", "")  # the docstring narrates the old bug
    assert "return None" not in body, "a None return is indistinguishable from a successful send"
    assert "raise TelegramError" in body


# --- flush must hold, not destroy, while she has not connected ----------------------------------

class _Row:
    def __init__(self, rid=1):
        self.id, self.buyer_id, self.property_id, self.reason = rid, 1, 1, "new"
        self.sent_at = self.failed_at = self.last_error = None
        self.attempts = 0
        self.telegram_message_id = None
        self.payload = None


class _Session:
    def __init__(self, channel):
        self.channel = channel

    def scalar(self, _stmt):
        return self.channel

    def flush(self):
        pass


def _channel():
    return SimpleNamespace(key="telegram", name="Notificações (Telegram)", kind="channel",
                           status="ok", last_ok_at=None, last_error=None, consecutive_failures=0)


def test_with_no_chat_id_the_rows_are_held_untouched(monkeypatch):
    """The exact bug: these rows must still be pending afterwards, not stamped as sent."""
    from app.services import notifications

    rows = [_Row(1), _Row(2)]
    channel = _channel()
    session = _Session(channel)

    monkeypatch.setattr(notifications, "pending", lambda _s: rows)
    monkeypatch.setattr(notifications.appsettings, "within_alert_hours", lambda _s, *a: True)
    monkeypatch.setattr(notifications.appsettings, "chat_id", lambda _s: None)

    def must_not_send(*_a, **_k):
        raise AssertionError("no send may be attempted without a chat id")

    monkeypatch.setattr(notifications.telegram, "send_message", must_not_send)

    assert notifications.flush(session) == 0
    assert all(r.sent_at is None for r in rows), "an alert was marked delivered with nothing sent"
    assert all(r.failed_at is None for r in rows), "waiting to connect must not retire an alert"
    assert all(r.attempts == 0 for r in rows), "waiting is not a delivery attempt"


def test_waiting_does_not_burn_attempts_over_many_flushes(monkeypatch):
    """At 30 attempts a row retires. The flush runs every 2 minutes, so charging attempts for a missing
    chat id would have destroyed every alert within an hour - the same loss by a slower route."""
    from app.services import notifications

    rows = [_Row()]
    session = _Session(_channel())
    monkeypatch.setattr(notifications, "pending", lambda _s: rows)
    monkeypatch.setattr(notifications.appsettings, "within_alert_hours", lambda _s, *a: True)
    monkeypatch.setattr(notifications.appsettings, "chat_id", lambda _s: None)

    for _ in range(notifications.MAX_ATTEMPTS + 5):
        notifications.flush(session)
    assert rows[0].attempts == 0 and rows[0].failed_at is None and rows[0].sent_at is None


def test_the_channel_shows_why_nothing_is_arriving(monkeypatch):
    from app.services import notifications

    channel = _channel()
    session = _Session(channel)
    monkeypatch.setattr(notifications, "pending", lambda _s: [_Row()])
    monkeypatch.setattr(notifications.appsettings, "within_alert_hours", lambda _s, *a: True)
    monkeypatch.setattr(notifications.appsettings, "chat_id", lambda _s: None)

    notifications.flush(session)
    assert channel.status == "degraded"
    assert channel.last_error == notifications.NO_CHAT_REASON


def test_the_banner_tells_her_to_tap_start_rather_than_reporting_a_fault(monkeypatch):
    """"os alertas não estão a ser entregues" reads as a broken system; the fix is one tap."""
    from app.services import health, notifications

    monkeypatch.setattr(health, "unhealthy", lambda session, kinds=None: [
        SimpleNamespace(name="Notificações (Telegram)", kind="channel", status="degraded",
                        last_error=notifications.NO_CHAT_REASON, last_ok_at=None)])
    monkeypatch.setattr(notifications, "undelivered_count", lambda _s: 12)

    class FakeResult:
        def all(self): return []

    class FakeSession:
        def execute(self, _stmt): return FakeResult()

    row = health.banner(FakeSession())[0]
    assert "Começar" in row["text"]
    assert "12" in row["text"]
    assert "não estão a ser entregues" not in row["text"]
    assert row["error"] is None, "the internal reason code must not be shown to her"


# --- the alert path must use the ADOPTED chat id, not only the .env fallback ---------------------

def test_flush_passes_the_database_chat_id_to_every_send():
    """`send_message` alone falls back to `settings.telegram_chat_id`, which is empty by design so her
    first /start can be adopted. Without passing the resolved id, buyer alerts would have kept going
    nowhere even AFTER she connected - while the digest, which does pass it, worked."""
    import inspect

    from app.services import notifications

    source = inspect.getsource(notifications.flush)
    assert "appsettings.chat_id(session)" in source, "the flush must resolve the adopted chat id"
    assert "chat_id=chat_id" in source, "and hand it to send_message"
