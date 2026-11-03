"""The daily digest: sent once a day, never twice, and a failure that is visible (plan section 4, layer 3).

`services/digest.py` had no test at all, and it is the one thing that reaches her every single morning.
Two specific risks it carries:

  * the once-a-day guard is real code but was never exercised, and the job ticks every 10 minutes - a
    broken guard means either a duplicate digest or a silently skipped day;
  * the send was unwrapped, so a transport failure propagated out of `send()` into the job, left the
    channel reporting `ok`, and said nothing in the panel. The digest is the SECOND delivery path and the
    only one that fails on a schedule.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.services import digest


class _Settings:
    """A stand-in for app_settings: a dict with the get/put the service uses."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.flushed = 0

    def get(self, _session, key, default=None):
        return self.values.get(key, default)

    def put(self, _session, key, value):
        self.values[key] = value


class _Session:
    def __init__(self, store: _Settings):
        self.store = store

    def flush(self):
        self.store.flushed += 1


@pytest.fixture
def wired(monkeypatch):
    """Everything the digest touches, stubbed, so only its own decisions are under test."""
    state = {"sent": [], "health": [], "chat": "123456", "raise_on_send": None}

    monkeypatch.setattr(digest, "per_buyer", lambda _s, _since: [("Rosa Brás", 2, 5, 9)])
    monkeypatch.setattr(digest, "degraded_agencies", lambda _s: 0)
    monkeypatch.setattr(digest, "panel_url", lambda: "https://painel/")
    monkeypatch.setattr(digest.formats, "digest", lambda *a, **k: "resumo")

    def fake_send(text, chat_id=None, **_k):
        if state["raise_on_send"]:
            raise RuntimeError(state["raise_on_send"])
        state["sent"].append((text, chat_id))
        return 42

    monkeypatch.setattr(digest.telegram, "send_message", fake_send)

    from app.services import notifications
    monkeypatch.setattr(notifications, "record_send_result",
                        lambda _s, ok, error="": state["health"].append((ok, error)))

    monkeypatch.setattr(digest.appsettings, "chat_id", lambda _s: state["chat"])
    monkeypatch.setattr(digest.appsettings, "tz", lambda: timezone.utc)
    return state


def _session(values=None):
    store = _Settings(values)
    session = _Session(store)
    return session, store


def test_the_digest_is_sent_once_and_the_day_is_recorded(wired, monkeypatch):
    session, store = _session()
    monkeypatch.setattr(digest.appsettings, "get", store.get)
    monkeypatch.setattr(digest.appsettings, "put", store.put)

    assert digest.send(session) is True
    assert len(wired["sent"]) == 1
    assert store.values[digest.LAST_SENT_KEY] == datetime.now(timezone.utc).date().isoformat()
    assert wired["health"] == [(True, "")], "a successful digest must clear the channel"


def test_a_second_tick_the_same_day_sends_nothing(wired, monkeypatch):
    """The job ticks every 10 minutes inside a 20-minute window, so this fires 2-3 times a morning."""
    today = datetime.now(timezone.utc).date().isoformat()
    session, store = _session({digest.LAST_SENT_KEY: today})
    monkeypatch.setattr(digest.appsettings, "get", store.get)
    monkeypatch.setattr(digest.appsettings, "put", store.put)

    assert digest.send(session) is False
    assert wired["sent"] == [], "she would have received two digests"


def test_yesterdays_mark_does_not_block_today(wired, monkeypatch):
    """The inverse failure: a stale mark that silently skips the day."""
    session, store = _session({digest.LAST_SENT_KEY: "2020-01-01"})
    monkeypatch.setattr(digest.appsettings, "get", store.get)
    monkeypatch.setattr(digest.appsettings, "put", store.put)

    assert digest.send(session) is True
    assert len(wired["sent"]) == 1


def test_force_overrides_the_guard(wired, monkeypatch):
    today = datetime.now(timezone.utc).date().isoformat()
    session, store = _session({digest.LAST_SENT_KEY: today})
    monkeypatch.setattr(digest.appsettings, "get", store.get)
    monkeypatch.setattr(digest.appsettings, "put", store.put)

    assert digest.send(session, force=True) is True
    assert len(wired["sent"]) == 1


def test_no_chat_id_yet_holds_the_digest_without_marking_the_day(wired, monkeypatch):
    """Until she presses Start there is no chat id. The day must NOT be marked, or the first digest after
    she connects would be skipped."""
    session, store = _session()
    monkeypatch.setattr(digest.appsettings, "get", store.get)
    monkeypatch.setattr(digest.appsettings, "put", store.put)
    wired["chat"] = None

    assert digest.send(session) is False
    assert digest.LAST_SENT_KEY not in store.values
    assert wired["sent"] == []


def test_a_failed_digest_is_recorded_retried_and_not_marked_sent(wired, monkeypatch):
    """It used to raise straight out of the job, leave the channel `ok`, and say nothing in the panel."""
    session, store = _session()
    monkeypatch.setattr(digest.appsettings, "get", store.get)
    monkeypatch.setattr(digest.appsettings, "put", store.put)
    wired["raise_on_send"] = "HTTP 403 Forbidden: bot was blocked by the user"

    assert digest.send(session) is False, "a failure must not report success"
    assert digest.LAST_SENT_KEY not in store.values, "an unsent digest must be retried"
    assert len(wired["health"]) == 1 and wired["health"][0][0] is False
    assert "403" in wired["health"][0][1], "the reason must reach the channel health"


def test_the_failure_does_not_escape_into_the_job(wired, monkeypatch):
    """`daily_digest` has no try/except of its own; an exception here kills the tick."""
    session, store = _session()
    monkeypatch.setattr(digest.appsettings, "get", store.get)
    monkeypatch.setattr(digest.appsettings, "put", store.put)
    wired["raise_on_send"] = "boom"

    digest.send(session)  # must simply return, not raise


@pytest.mark.parametrize("now_minutes, target, expected", [
    (8 * 60 + 30, (8, 30), True),    # exactly her hour
    (8 * 60 + 45, (8, 30), True),    # inside the 20-minute slack
    (8 * 60 + 55, (8, 30), False),   # past it
    (8 * 60 + 20, (8, 30), False),   # before it
])
def test_the_digest_window_is_her_hour_plus_a_restart_allowance(now_minutes, target, expected, monkeypatch):
    """The slack is what stops a restart across her hour from skipping the day entirely."""
    import datetime as dt

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 4, now_minutes // 60, now_minutes % 60, tzinfo=timezone.utc)

    monkeypatch.setattr(digest, "datetime", _Now)
    monkeypatch.setattr(digest.appsettings, "tz", lambda: timezone.utc)
    monkeypatch.setattr(digest.appsettings, "digest_at", lambda _s: dt.time(*target))
    assert digest.due_now(object()) is expected
