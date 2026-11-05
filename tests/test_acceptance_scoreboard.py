"""The acceptance number decides payment, so its three exclusions are tested, not assumed.

The contract: "at least 80 % of the properties she finds manually, counting only properties published in
the window and present in the agreed sources". The risk register of the plan names the denominator - not
the system - as risk number one, and these are the ways the denominator can be wrong.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.web.routers.findings import score


def row(verdict: str, in_window: bool | None, buyer: str | None = None) -> dict:
    return {"finding": SimpleNamespace(verdict=verdict, published_in_window=in_window),
            "buyer": SimpleNamespace(label=buyer) if buyer else None, "listing": None}


def test_out_of_scope_sources_leave_the_denominator():
    # Scoring the system against Casafari or SUPERCASA would be scoring work never contracted.
    s = score([row("ja_tinha", True), row("fora_do_ambito", None), row("fora_do_ambito", True)])
    assert (s["eligible"], s["excluded"], s["pct"]) == (1, 2, 1.0)


def test_published_before_the_window_leaves_the_denominator():
    # The system cannot be blamed for inventory that existed before it was switched on.
    s = score([row("ja_tinha", True), row("fonte_acordada", False)])
    assert s["eligible"] == 1 and s["excluded"] == 1 and s["pct"] == 1.0


def test_an_undated_finding_is_pending_and_moves_the_number_in_neither_direction():
    decided = score([row("ja_tinha", True), row("fonte_acordada", True)])
    with_pending = score([row("ja_tinha", True), row("fonte_acordada", True),
                          row("fonte_acordada", None), row("ja_tinha", None)])
    assert with_pending["pct"] == decided["pct"] == 0.5
    assert with_pending["pending"] == 2
    assert with_pending["eligible"] == 2


def test_a_miss_from_an_agreed_source_inside_the_window_is_what_costs_us():
    s = score([row("ja_tinha", True)] * 4 + [row("fonte_acordada", True)])
    assert s["pct"] == 0.8 and s["eligible"] == 5


def test_no_eligible_findings_reports_no_score_rather_than_zero_or_one_hundred():
    # An empty scoreboard must not read as "0 % coverage" during the first hours of the test.
    assert score([])["pct"] is None
    assert score([row("fora_do_ambito", None)])["pct"] is None


def test_window_is_inclusive_on_both_days(monkeypatch):
    from app.services import appsettings

    start = date(2026, 11, 7)
    captured: dict = {}

    def fake_get(_session, key, default=None):
        return captured.get(key, default)

    def fake_put(_session, key, value):
        captured[key] = value

    monkeypatch.setattr(appsettings, "get", fake_get)
    monkeypatch.setattr(appsettings, "put", fake_put)
    first, last = appsettings.set_test_window(None, start, 7)
    assert (first, last) == (start, date(2026, 11, 13))          # 7 days, not 8
    assert appsettings.test_window(None) == (first, last)

    def at(day: date):
        return datetime(day.year, day.month, day.day, 12, 0, tzinfo=timezone.utc)

    assert appsettings.in_test_window(None, at(first)) is True   # first day counts
    assert appsettings.in_test_window(None, at(last)) is True    # last day counts
    assert appsettings.in_test_window(None, at(first - timedelta(days=1))) is False
    assert appsettings.in_test_window(None, at(last + timedelta(days=1))) is False


def test_a_listing_we_never_held_cannot_be_dated_and_says_so(monkeypatch):
    from app.services import appsettings
    monkeypatch.setattr(appsettings, "get", lambda *a, **k: {"start": "2026-11-07", "days": 7})
    assert appsettings.in_test_window(None, None) is None


def test_without_a_window_nothing_can_be_dated(monkeypatch):
    from app.services import appsettings
    monkeypatch.setattr(appsettings, "get", lambda *a, **k: None)
    assert appsettings.in_test_window(None, datetime.now(timezone.utc)) is None


class _Buyers:
    """Minimal stand-in for the session: `_match_buyer` only ever scans the buyer list."""

    def __init__(self, *labels):
        self._rows = [SimpleNamespace(id=i + 1, label=label) for i, label in enumerate(labels)]

    def scalars(self, _stmt):
        return self._rows


def test_her_shorthand_finds_the_buyer():
    from app.bot.handlers.findings import _match_buyer
    session = _Buyers("Rosa Brás", "Raquel Gonçalves")
    assert _match_buyer(session, "Rosa").label == "Rosa Brás"
    assert _match_buyer(session, "raquel").label == "Raquel Gonçalves"
    assert _match_buyer(session, "Gonçalves").label == "Raquel Gonçalves"


def test_an_ambiguous_or_absent_name_attaches_to_nobody():
    # Attaching a finding to the wrong buyer corrupts the very number it exists to measure, so two
    # matches is not a match.
    from app.bot.handlers.findings import _match_buyer
    assert _match_buyer(_Buyers("Ana Silva", "Ana Costa"), "Ana") is None
    assert _match_buyer(_Buyers("Rosa Brás"), "Teresa") is None
    assert _match_buyer(_Buyers("Rosa Brás"), "") is None
