"""Day 7: the spend cap, and what it must NOT switch off.

The promise was a ceiling on the AI, not on the search. These tests pin the separation: the cap lives in one
function, and the crawl/match/alert modules must stay free of it. `scripts/smoke_day7.py` proves the same
thing end to end against real data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.services import budget


class _Session:
    """Minimal stand-in: `app_settings` reads plus one scalar for the ledger sum."""

    def __init__(self, stored: dict | None = None, spent: int = 0):
        self.stored = dict(stored or {})
        self.spent = spent
        self.added = []

    def get(self, _model, key):
        return SimpleNamespace(value=self.stored[key]) if key in self.stored else None

    def scalar(self, *_args, **_kwargs):
        return self.spent

    def add(self, row):
        # `appsettings.put` inserts an AppSetting row when the key is absent; the stub has to honour that,
        # otherwise "warn once per month" can never be observed.
        self.added.append(row)
        key = getattr(row, "key", None)
        if key is not None:
            self.stored[key] = getattr(row, "value", None)

    def flush(self):
        pass


def test_the_cap_comes_from_the_database_so_she_can_change_it_herself():
    assert budget.cap_cents(_Session({budget.CAP_KEY: 1200})) == 1200


def test_a_missing_or_broken_cap_falls_back_to_the_env_default():
    from app.config import settings

    assert budget.cap_cents(_Session()) == settings.ai_cap_cents_month
    assert budget.cap_cents(_Session({budget.CAP_KEY: "muito"})) == settings.ai_cap_cents_month


def test_states_are_ok_then_warning_then_suspended():
    assert budget.status(_Session({budget.CAP_KEY: 100}, spent=10))["state"] == budget.STATE_OK
    assert budget.status(_Session({budget.CAP_KEY: 100}, spent=80))["state"] == budget.STATE_WARNING
    assert budget.status(_Session({budget.CAP_KEY: 100}, spent=100))["state"] == budget.STATE_SUSPENDED
    assert budget.status(_Session({budget.CAP_KEY: 100}, spent=250))["state"] == budget.STATE_SUSPENDED


def test_a_zero_cap_means_no_cap_at_all():
    session = _Session({budget.CAP_KEY: 0}, spent=999_999)
    assert budget.status(session)["state"] == budget.STATE_OK
    budget.ensure_within(session)      # must not raise


def test_the_guard_raises_at_the_cap_not_merely_over_it():
    session = _Session({budget.CAP_KEY: 100}, spent=100)
    try:
        budget.ensure_within(session)
        raise AssertionError("expected BudgetExceeded exactly at the cap")
    except budget.BudgetExceeded as exc:
        assert "limite" in str(exc).lower()


def test_an_expensive_call_is_refused_before_it_is_made():
    """`estimate_cents` is what stops a single large call from sailing past the ceiling."""
    session = _Session({budget.CAP_KEY: 100}, spent=60)
    budget.ensure_within(session)                       # fine on its own
    try:
        budget.ensure_within(session, estimate_cents=50)
        raise AssertionError("expected the estimate to be counted")
    except budget.BudgetExceeded:
        pass


def test_the_warning_fires_once_per_calendar_month(monkeypatch):
    sent = []
    monkeypatch.setattr("app.notify.telegram.send_message", lambda text, **kw: sent.append(text))
    session = _Session({budget.CAP_KEY: 100}, spent=85)
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    budget.ensure_within(session, now=now)
    budget.ensure_within(session, now=now)
    assert len(sent) == 1, sent
    assert session.stored[budget.WARNED_KEY] == "2026-10"
    # A new month warns again.
    budget.ensure_within(session, now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert len(sent) == 2


def test_the_warning_explains_that_the_search_keeps_working(monkeypatch):
    """If she reads "limite atingido" and thinks the system stopped, the wording failed."""
    sent = []
    monkeypatch.setattr("app.notify.telegram.send_message", lambda text, **kw: sent.append(text))
    budget.ensure_within(_Session({budget.CAP_KEY: 100}, spent=90),
                         now=datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert "continuam a funcionar" in sent[0]
    assert "painel" in sent[0]


def test_a_telegram_failure_does_not_break_the_call_that_triggered_it(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("app.notify.telegram.send_message", boom)
    budget.ensure_within(_Session({budget.CAP_KEY: 100}, spent=85),
                         now=datetime(2026, 10, 31, tzinfo=timezone.utc))


def test_the_cap_is_enforced_in_exactly_one_place():
    """Grep is the test here: a second enforcement point is how "the AI stopped the crawl" happens."""
    import pathlib

    callers = []
    for path in pathlib.Path("app").rglob("*.py"):
        body = path.read_text(encoding="utf-8")
        if "ensure_within(" in body or "budget_guard(" in body:
            callers.append(path.as_posix())
    assert "app/ai/client.py" in callers
    for path in callers:
        assert path.startswith("app/ai/") or path == "app/services/budget.py", path


def test_crawling_and_alerting_never_import_the_budget():
    """The import is what matters, not the word: `agencies.py` legitimately discusses "the watch budget"."""
    import pathlib
    import re

    pattern = re.compile(r"^\s*(from app\.services import .*budget|from app\.services\.budget import|import app\.services\.budget)",
                         re.M)
    for name in ("services/runs.py", "services/notifications.py", "services/properties.py",
                 "services/matches.py", "services/agencies.py", "sources/imovirtual.py", "sources/remax.py"):
        body = pathlib.Path("app", name).read_text(encoding="utf-8")
        assert not pattern.search(body), name


def test_money_formatting_is_readable():
    assert budget.human(500) == "5.00 USD"
    assert budget.human(0) == "0.00 USD"
    assert budget.human(None) == "—"
