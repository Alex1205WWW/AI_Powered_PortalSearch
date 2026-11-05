"""A bot with no token must say so where someone looks, not only in a log line.

QA checklist item 4.3. Before this, `TELEGRAM_BOT_TOKEN` missing meant: one warning on stdout, a
container reading a plain "Up" under `docker compose ps`, a channel source row still saying `ok`, and not
one message able to leave the system. That is the failure mode that looks healthiest from the outside -
the same class as the 96 alerts destroyed on 2026-11-04, where every surface reported fine.

Two reporting surfaces, because they answer different questions:
  the channel source row   -> the panel banner, which is what SHE sees
  the compose healthcheck  -> "docker compose ps", which is what whoever maintains it sees

Deliberately NOT a delivery failure: no send was attempted, so nothing may consume an alert's attempts.
At MAX_ATTEMPTS = 30 and a two-minute flush, counting it as a failure would retire every queued alert
within an hour - the loss this project already paid for once.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


class _Session:
    """Just enough session for `_channel()`: it only ever does `session.scalar(select(Source))`."""

    def __init__(self, channel):
        self.channel = channel
        self.committed = False

    def scalar(self, _stmt):
        return self.channel

    def flush(self):
        pass

    def commit(self):
        self.committed = True


def _channel(**over):
    row = dict(key="telegram", name="Notificações (Telegram)", kind="channel", status="ok",
               last_ok_at=None, last_error=None, consecutive_failures=0)
    row.update(over)
    return SimpleNamespace(**row)


# --- the channel row carries the fault ----------------------------------------------------------

def test_a_missing_token_degrades_the_channel_with_its_own_reason():
    from app.services import notifications

    channel = _channel()
    notifications.mark_channel_token_missing(_Session(channel))
    assert channel.status == "degraded"
    assert channel.last_error == notifications.NO_TOKEN_REASON


def test_reporting_a_missing_token_never_counts_as_a_delivery_failure():
    """The idle loop re-asserts this every hour. Going through `record_failure` would count every tick,
    and a counted failure is what retires queued alerts - so this must leave the counter alone."""
    from app.services import notifications

    channel = _channel()
    session = _Session(channel)
    for _ in range(40):
        notifications.mark_channel_token_missing(session)
    assert channel.consecutive_failures == 0, "re-asserting the token state must not count as failures"
    assert channel.status == "degraded"
    assert channel.last_error == notifications.NO_TOKEN_REASON


def test_an_unseeded_channel_row_is_not_an_error():
    """A fresh database before the seed runs: reporting a fault must never become one."""
    from app.services import notifications

    notifications.mark_channel_token_missing(_Session(None))
    notifications.clear_channel_token_missing(_Session(None))


# --- and it is cleared only when its cause is gone -----------------------------------------------

def test_a_configured_token_clears_the_stored_no_token_fault():
    from app.services import notifications

    channel = _channel(status="degraded", last_error=notifications.NO_TOKEN_REASON, consecutive_failures=3)
    notifications.clear_channel_token_missing(_Session(channel))
    assert channel.status == "ok"
    assert channel.last_error is None
    assert channel.consecutive_failures == 0


def test_a_real_delivery_failure_is_not_cleared_by_a_bot_restart():
    """Only the fault this module wrote may be cleared here. A channel broken by a refused send is
    cleared by a send that succeeds - otherwise restarting the bot would hide a live outage."""
    from app.services import notifications

    channel = _channel(status="degraded", last_error="HTTP 403 Forbidden: bot was blocked by the user",
                       consecutive_failures=7)
    notifications.clear_channel_token_missing(_Session(channel))
    assert channel.status == "degraded"
    assert channel.consecutive_failures == 7


def test_waiting_for_her_to_press_start_is_not_cleared_either():
    from app.services import notifications

    channel = _channel(status="degraded", last_error=notifications.NO_CHAT_REASON)
    notifications.clear_channel_token_missing(_Session(channel))
    assert channel.status == "degraded"
    assert channel.last_error == notifications.NO_CHAT_REASON


# --- the banner says the true thing, which is a third wording ------------------------------------

def _banner_row(monkeypatch, reason, waiting=0):
    from app.services import health, notifications

    monkeypatch.setattr(health, "unhealthy", lambda session, kinds=None: [
        SimpleNamespace(name="Notificações (Telegram)", kind="channel", status="degraded",
                        last_error=reason, last_ok_at=None)])
    monkeypatch.setattr(notifications, "undelivered_count", lambda _s: waiting)

    class FakeResult:
        def all(self):
            return []

    class FakeSession:
        def execute(self, _stmt):
            return FakeResult()

    return health.banner(FakeSession())[0]


def test_the_banner_does_not_ask_her_to_tap_start_when_the_token_is_missing(monkeypatch):
    """"Toque em Começar" is the fix for a missing chat id. With no token, tapping does nothing at all,
    and sending her chasing a button that cannot work is worse than saying nothing."""
    from app.services import notifications

    row = _banner_row(monkeypatch, notifications.NO_TOKEN_REASON, waiting=5)
    assert "Começar" not in row["text"]
    assert "painel" in row["text"], "she must still be told the opportunities are not lost"
    assert "5" in row["text"], "the queue depth is the one number that tells her what is at stake"
    assert "TELEGRAM_BOT_TOKEN" in (row["error"] or ""), "the maintainer needs the actual cause named"


def test_the_three_channel_faults_read_differently(monkeypatch):
    """Regression guard on the branch order: a new reason added above the others must not swallow them."""
    from app.services import notifications

    texts = {}
    for reason in (notifications.NO_TOKEN_REASON, notifications.NO_CHAT_REASON, "HTTP 500"):
        texts[reason] = _banner_row(monkeypatch, reason, waiting=3)["text"]
    assert len(set(texts.values())) == 3, f"two channel faults read the same: {texts}"


# --- the bot entrypoint actually reports, on both paths ------------------------------------------

def test_the_idle_loop_reports_the_missing_token_on_every_tick(monkeypatch):
    """Once at startup is not enough: the channel row is shared state, and a later write by something
    else would leave the panel claiming delivery works while this container still has no token."""
    from app.bot import main as bot_main

    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "")
    monkeypatch.setattr(bot_main, "setup_logging", lambda *_a, **_k: None)
    reported = []
    monkeypatch.setattr(bot_main, "_report_token_state", lambda missing: reported.append(missing))

    ticks = []

    def fake_sleep(seconds):
        ticks.append(seconds)
        if len(ticks) >= 3:
            raise KeyboardInterrupt  # the only way out of a deliberate forever-loop

    monkeypatch.setattr(bot_main.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        bot_main.main()

    assert reported == [True, True, True]
    assert ticks == [bot_main.IDLE_REPORT_S] * 3


def test_a_token_being_present_clears_the_fault_before_polling_starts(monkeypatch):
    from app.bot import main as bot_main

    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(bot_main, "setup_logging", lambda *_a, **_k: None)
    order = []
    monkeypatch.setattr(bot_main, "_report_token_state", lambda missing: order.append(("report", missing)))
    monkeypatch.setattr(bot_main, "build_application",
                        lambda: SimpleNamespace(run_polling=lambda **_k: order.append(("poll", None))))

    bot_main.main()
    assert order == [("report", False), ("poll", None)], "the stale fault must be cleared before polling"


def test_a_database_that_is_down_does_not_stop_the_bot(monkeypatch):
    """The report is a courtesy; it may never be the reason the bot fails to start."""
    import app.db.session as db_session
    from app.bot import main as bot_main

    def boom():
        raise RuntimeError("could not connect to server")

    monkeypatch.setattr(db_session, "session_scope", boom)
    bot_main._report_token_state(missing=True)  # must not raise


# --- and the container stops reading a plain "Up" ------------------------------------------------

def test_the_bot_container_has_a_healthcheck():
    """`docker compose ps` is where this was invisible. A service with no healthcheck can only ever
    report "Up", however broken the process inside it is."""
    import yaml

    with open("docker-compose.yml", encoding="utf-8") as handle:
        compose = yaml.safe_load(handle)
    check = compose["services"]["bot"].get("healthcheck")
    assert check, "the bot service must declare a healthcheck"
    probe = " ".join(check["test"])
    assert "TELEGRAM_BOT_TOKEN" in probe, "the probe must fail exactly when the bot cannot work"
