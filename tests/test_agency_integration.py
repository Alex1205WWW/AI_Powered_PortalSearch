"""The seven integration points Day 5 touched (`Milestone1/Plan_Day5.md` §5).

Five were real breakage once sources are created at runtime instead of being a fixed list of eight; two were
usability. The last test pins the one that needed **no** change, so a later session does not "fix" it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.client import AiGuardError, assert_llm_allowed, assert_source_allows_llm
from app.domain.listing import (AGENCY_CANARY_THRESHOLDS, CANARY_MIN_SAMPLE, CANARY_THRESHOLDS, CanonicalListing,
                                FillStats, canary_fields, canary_fields_for, canary_thresholds_for)
from app.notify.formats import source_label
from app.services import agencies as agency_svc
from app.services import health


def listing(**kwargs) -> CanonicalListing:
    base = dict(source_key="agency:test", external_id="1", url="https://x.pt/1", title="Apartamento T2",
                price=300_000, rooms=2, agency_name="Mediadora X")
    base.update(kwargs)
    return CanonicalListing(**base)


# --- 1. the canary (blocker) --------------------------------------------------------------------

def test_a_field_the_profile_never_promised_is_not_drift():
    """eGO types no area at all. Holding it to the Imovirtual contract would flip every eGO source to
    `degraded` on its first run - and, before the health fix, send a Telegram message for each."""
    expect = canary_fields(["title", "price", "typology"])          # no "area", no "published"
    stats = FillStats(expect)
    for _ in range(CANARY_MIN_SAMPLE + 5):
        stats.add(listing(area_util=None, published_at=None))
    assert stats.ratios()["area"] == 0.0        # still measured and visible
    assert stats.breaches() == []               # but not called drift


def test_a_field_the_profile_did_promise_still_breaches():
    stats = FillStats(canary_fields(["title", "price", "typology", "area"]))
    for _ in range(CANARY_MIN_SAMPLE + 5):
        stats.add(listing(area_util=None))
    assert any(b.startswith("area") for b in stats.breaches())


def test_a_missing_advertiser_is_not_drift_on_an_agency_site():
    """The advertiser on an agency site is the site itself. Measured 2026-11-02: all six JSON-LD sites carry no
    per-advert agent block (comprarcasa.pt 0 % on 702 correctly-read listings) while every eGO site does. The
    old rule put a false "problemas" banner on every page; the agency is known from the `sources` row."""
    stats = FillStats(canary_fields(["title", "price", "typology"]), thresholds=canary_thresholds_for("agency"))
    for _ in range(CANARY_MIN_SAMPLE + 5):
        stats.add(listing(agency_name=None))
    assert stats.ratios()["agency"] == 0.0     # still measured and visible
    assert stats.breaches() == []              # but not called drift


def test_portals_still_require_the_advertiser():
    stats = FillStats()                        # portal contract
    for _ in range(CANARY_MIN_SAMPLE + 5):
        stats.add(listing(agency_name=None))
    assert any(b.startswith("agency") for b in stats.breaches())


def test_an_agency_catalogue_is_judged_by_half_not_by_the_portal_bar():
    """comprarcasa.pt reads correctly and fills `rooms` on 54 % of its mixed catalogue (houses are often
    listed without a T-number). 90 % is a portal number; a layout change takes a field to ~0 %, which half
    still catches."""
    assert canary_thresholds_for("agency") is AGENCY_CANARY_THRESHOLDS
    assert canary_thresholds_for("portal") is CANARY_THRESHOLDS
    stats = FillStats(canary_fields(["title", "price", "typology"]), thresholds=canary_thresholds_for("agency"))
    for i in range(100):
        stats.add(listing(rooms=2 if i < 54 else None))
    assert stats.breaches() == []
    collapsed = FillStats(canary_fields(["title", "price", "typology"]), thresholds=canary_thresholds_for("agency"))
    for _ in range(100):
        collapsed.add(listing(rooms=None))
    assert any(b.startswith("rooms 0%") for b in collapsed.breaches())


def test_a_plot_has_no_typology_and_its_size_is_land_area():
    """easygest.com.pt lists 189 plots: no T-number is the truth about a terreno, not a field the page withheld."""
    stats = FillStats(canary_fields(["title", "price", "typology", "area"]), thresholds=canary_thresholds_for("agency"))
    for _ in range(CANARY_MIN_SAMPLE + 5):
        stats.add(listing(rooms=None, area_util=None, land_area=1200.0, estate_type="terreno"))
    assert stats.breaches() == []


def test_portals_keep_the_full_contract():
    assert canary_fields_for("portal", None) is None
    assert canary_fields_for("email", {"fields_present": ["title"]}) is None
    assert canary_fields_for("agency", {"fields_present": ["title", "area"]}) == {"area"}


# --- 2. the AI guard (blocker) ------------------------------------------------------------------

def test_a_runtime_source_declaring_ai_input_no_is_refused():
    """A hard-coded set of source keys cannot cover the agency sources, which are created while running."""
    source = SimpleNamespace(key="agency:somewhere.pt", signal_ai_input="no")
    with pytest.raises(AiGuardError):
        assert_source_allows_llm(source, purpose="profile")


def test_a_runtime_source_that_allows_ai_input_passes():
    allowed = SimpleNamespace(key="agency:dils.pt", signal_ai_input="yes")       # real signal, 2026-10-31
    silent = SimpleNamespace(key="agency:residential.jll.pt", signal_ai_input=None)
    assert_source_allows_llm(allowed, purpose="profile")
    assert_source_allows_llm(silent, purpose="profile")


def test_imovirtual_stays_refused_by_the_literal_as_well():
    """Belt and braces: the directory and profile pages are Imovirtual content too."""
    with pytest.raises(AiGuardError):
        assert_llm_allowed("imovirtual", purpose="profile")
    with pytest.raises(AiGuardError):
        assert_source_allows_llm(SimpleNamespace(key="imovirtual", signal_ai_input=None), purpose="profile")


# --- 3 & 4. health: no Telegram flood, no banner flood -----------------------------------------

def test_agency_sources_do_not_each_send_a_telegram_message(monkeypatch):
    """They mostly run the same eGO software, so one platform change degrades them together."""
    sent = []
    monkeypatch.setattr("app.notify.telegram.send_message", lambda text, **kw: sent.append(text))

    agency = SimpleNamespace(key="agency:x.pt", name="X", kind="agency", status="ok",
                             last_ok_at=None, last_error=None, consecutive_failures=0)
    health.degrade(agency, "selector mudou")
    assert agency.status == "degraded"       # still recorded
    assert sent == []                        # but she is not messaged about each one

    portal = SimpleNamespace(key="imovirtual", name="Imovirtual", kind="portal", status="ok",
                             last_ok_at=None, last_error=None, consecutive_failures=0)
    health.degrade(portal, "canary: area 10% < 90%")
    assert len(sent) == 1 and "Imovirtual" in sent[0]


def test_the_grouped_banner_says_what_is_happening_not_that_sites_are_down(monkeypatch):
    """Seen 2026-11-02 on production: "9 mediadoras com problemas não está a responder" - nine working
    sites, read with fewer fields than promised, presented as nine outages. The row carries its own
    sentence so base.html does not append the portal wording to it."""
    monkeypatch.setattr(health, "unhealthy", lambda session, kinds=None: [])

    class FakeResult:
        def __init__(self, rows): self._rows = rows
        def all(self): return self._rows

    class FakeSession:
        def execute(self, stmt): return FakeResult([("degraded", 3), ("blocked", 2)])

    rows = health.banner(FakeSession())
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Sites de mediadoras"
    assert row["text"] == ("3 com leitura incompleta e 2 bloqueados pelo próprio site. "
                           "Os anúncios já recolhidos continuam no painel.")
    assert "não está a responder" not in row["text"]
    assert row["agency_count"] == 5

    class QuietSession:
        def execute(self, stmt): return FakeResult([])

    assert health.banner(QuietSession()) == []


def test_the_offportal_share_refuses_to_speak_on_a_small_sample():
    """Measured 2026-11-02: dividing a whole catalogue by "no portal listing in our tables" read 100 % for
    nearly every agency, because our portal coverage is a slice. The share is now taken over new in-scope
    stock only, and a handful of listings is not a percentage."""
    assert agency_svc.offportal_share(0, 0) is None
    assert agency_svc.offportal_share(9, 9) is None
    assert agency_svc.offportal_share(10, 9) == 0.9
    assert agency_svc.offportal_share(161, 151) == 0.9379
    assert agency_svc.offportal_share(36, 0) == 0.0
    assert agency_svc.OFFPORTAL_MIN_SAMPLE == 10


# --- 5. the removal guard -----------------------------------------------------------------------

def test_a_short_sitemap_never_retires_a_catalogue():
    """eGO has no <lastmod>, so removal is a set-difference - and a truncated sitemap looks exactly like a
    mass withdrawal. The guard is what stops one bad fetch deleting her results."""
    source = SimpleNamespace(key="agency:x.pt", last_catalogue_size=1000)
    assert agency_svc._removal_guard(source, catalogue_size=400, known_size=1000).startswith("blocked")
    assert agency_svc._removal_guard(source, catalogue_size=0, known_size=1000).startswith("blocked")
    assert agency_svc._removal_guard(source, catalogue_size=950, known_size=1000) == "allowed"


def test_the_guard_uses_what_we_know_when_the_source_is_new():
    source = SimpleNamespace(key="agency:x.pt", last_catalogue_size=None)
    assert agency_svc._removal_guard(source, catalogue_size=10, known_size=1000).startswith("blocked")
    assert agency_svc._removal_guard(source, catalogue_size=1000, known_size=0) == "allowed"


# --- 6. link labels -----------------------------------------------------------------------------

def test_an_agency_link_shows_its_host_not_the_word_fonte():
    assert source_label("agency:quintelaepenalva.pt") == "quintelaepenalva.pt"
    assert source_label("agency:www.portadafrente.com") == "portadafrente.com"
    assert source_label("imovirtual") == "Imovirtual"
    assert source_label(None) == "Fonte"


def test_a_source_key_fits_the_column_even_for_a_long_host():
    long_host = "https://www." + ("a" * 90) + ".pt"
    key = agency_svc.source_key_for(long_host)
    assert len(key) <= agency_svc.SOURCE_KEY_MAX
    assert agency_svc.source_key_for("https://www.dils.pt/") == "agency:dils.pt"
    # Different long hosts must not collapse onto one key.
    assert key != agency_svc.source_key_for("https://www." + ("b" * 90) + ".pt")


# --- 7. the one that needed no change -----------------------------------------------------------

def test_the_scheduler_never_gains_a_job_per_agency():
    """`enabled_portal_schedules` and `catalogue_sources` both filter `kind == 'portal'`, so hundreds of
    agency rows cannot become hundreds of APScheduler timers. Pinned so it is not "fixed" later."""
    import inspect

    from app.worker import main

    for function in (main.enabled_portal_schedules, main.catalogue_sources):
        assert 'kind == "portal"' in inspect.getsource(function)
