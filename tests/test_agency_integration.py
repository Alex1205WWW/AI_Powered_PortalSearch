"""The seven integration points Day 5 touched (`Milestone1/Plan_Day5.md` §5).

Five were real breakage once sources are created at runtime instead of being a fixed list of eight; two were
usability. The last test pins the one that needed **no** change, so a later session does not "fix" it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai.client import AiGuardError, assert_llm_allowed, assert_source_allows_llm
from app.domain.listing import CANARY_MIN_SAMPLE, CanonicalListing, FillStats, canary_fields, canary_fields_for
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


def test_a_missing_advertiser_is_always_drift_on_an_agency_site():
    """The advertiser on an agency site is the site owner, so a listing without one is a parse failure."""
    stats = FillStats(canary_fields(["title", "price", "typology"]))
    for _ in range(CANARY_MIN_SAMPLE + 5):
        stats.add(listing(agency_name=None))
    assert any(b.startswith("agency") for b in stats.breaches())


def test_portals_keep_the_full_contract():
    assert canary_fields_for("portal", None) is None
    assert canary_fields_for("email", {"fields_present": ["title"]}) is None
    assert canary_fields_for("agency", {"fields_present": ["title", "area"]}) == {"agency", "area"}


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
