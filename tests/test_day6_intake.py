"""Day 6: what the model is allowed to decide, what it costs, and when her phone is allowed to buzz.

The DB-backed paths (drafts, buyer matching, the full loop) are exercised live against the dev database by
`scripts/smoke_day6.py`, following the project's established split: pure logic here, real data there.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.ai import pricing
from app.ai.parse_criteria import ParsedCriteria, sanitise, system_prompt
from app.notify import formats
from app.services import appsettings


# --- what a call costs --------------------------------------------------------------------------

def test_opus_and_sonnet_rates_are_the_published_ones():
    """$5/$25 and $2/$10 per MTok. A wrong rate here makes her monthly cap meaningless."""
    assert pricing.llm_rate("claude-opus-5").input_usd == 5.00
    assert pricing.llm_rate("claude-opus-5").output_usd == 25.00
    assert pricing.llm_rate("claude-sonnet-5").input_usd == 2.00
    assert pricing.llm_rate("claude-sonnet-5").output_usd == 10.00


def test_an_unknown_model_is_costed_at_the_most_expensive_rate():
    """A typo in LLM_MODEL must over-report spend, never under-report it against the cap."""
    assert pricing.llm_cost_cents("claude-made-up", tokens_in=1_000_000, tokens_out=0) >= \
           pricing.llm_cost_cents("claude-opus-5", tokens_in=1_000_000, tokens_out=0)


def test_a_typical_criteria_parse_costs_about_a_cent():
    """~700 in / ~180 out on Opus 5. This is the number behind "parsing is ~1.3 USD/month at 100 parses"."""
    assert pricing.llm_cost_cents("claude-opus-5", tokens_in=700, tokens_out=180) == 1


def test_cost_is_rounded_up_so_small_calls_are_never_free():
    assert pricing.llm_cost_cents("claude-opus-5", tokens_in=1, tokens_out=1) == 1
    assert pricing.stt_cost_cents(30) == 1          # 30 s of audio
    assert pricing.stt_cost_cents(0) == 0


def test_cache_reads_are_cheaper_than_fresh_input():
    fresh = pricing.llm_cost_cents("claude-opus-5", tokens_in=100_000, tokens_out=0)
    cached = pricing.llm_cost_cents("claude-opus-5", tokens_in=0, tokens_out=0, cache_read=100_000)
    assert cached < fresh


# --- the model's output is not trusted as-is ----------------------------------------------------

def test_invented_features_are_dropped_and_reported():
    """The schema constrains shape, not values: the model can still answer "garagem dupla"."""
    parsed, notes = sanitise(ParsedCriteria(must_have=["garagem", "garagem dupla"], nice_to_have=["heliporto"]))
    assert parsed.must_have == ["garagem"]
    assert parsed.nice_to_have == []
    assert any("garagem dupla" in n for n in notes)
    assert any("heliporto" in n for n in notes)


def test_a_feature_cannot_be_both_required_and_desirable():
    parsed, _notes = sanitise(ParsedCriteria(must_have=["elevador"], nice_to_have=["elevador"]))
    assert parsed.must_have == ["elevador"]
    assert parsed.nice_to_have == []


def test_inverted_ranges_are_corrected_visibly():
    parsed, notes = sanitise(ParsedCriteria(price_min=450000, price_max=350000,
                                            typology_min=3, typology_max=1))
    assert (parsed.price_min, parsed.price_max) == (350000, 450000)
    assert (parsed.typology_min, parsed.typology_max) == (1, 3)
    assert len(notes) == 2      # both swaps are reported, not silent


def test_an_unknown_estate_type_falls_back_to_apartamento():
    parsed, notes = sanitise(ParsedCriteria(estate_types=["castelo"]))
    assert parsed.estate_types == ["apartamento"]
    assert notes


def test_rent_is_preserved_but_nonsense_is_not():
    assert sanitise(ParsedCriteria(transaction="rent"))[0].transaction == "rent"
    assert sanitise(ParsedCriteria(transaction="trocar"))[0].transaction == "sale"


def test_the_prompt_pins_the_closed_vocabulary_into_itself():
    """The model is told the exact keys, so "garagem" comes back rather than "parking"."""
    prompt = system_prompt()
    assert "garagem" in prompt and "elevador" in prompt and "apartamento" in prompt
    assert "T1+1" in prompt          # the compound-typology rule is stated
    assert "ambiguities" in prompt   # asking is explicitly preferred to guessing


# --- the confirmation card has to look wrong when it is wrong -----------------------------------

def test_the_card_shows_every_ambiguity_as_a_question():
    card = formats.criteria_card(
        {"buyer_label": "Ana Santos", "estate_types": ["apartamento"], "typology_min": 1, "typology_max": 2,
         "price_min": 300000, "price_max": 400000, "transaction": "sale"},
        ['"Lisboa" pode ser distrito ou concelho. Qual deles?'], [])
    assert "Preciso de confirmar" in card
    assert "Lisboa" in card


def test_a_card_without_zones_says_so_rather_than_looking_complete():
    card = formats.criteria_card({"buyer_label": "X", "estate_types": ["moradia"]}, [], [])
    assert "por definir" in card


def test_the_card_is_html_escaped():
    card = formats.criteria_card({"buyer_label": "<b>injected</b>", "estate_types": ["apartamento"]}, [], [])
    assert "&lt;b&gt;injected" in card


def test_the_first_list_is_promised_to_the_panel_not_to_telegram():
    """300 messages on day one would kill adoption - this wording is a commitment to her."""
    text = formats.confirmed_text("Ana Santos", "https://painel/")
    assert "painel" in text.lower()


# --- keyboards ----------------------------------------------------------------------------------

def test_every_button_fits_telegram_callback_data_limit():
    keyboard = formats.state_keyboard([(999_999_999, "Rosa Brás")])
    payloads = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
    assert all(len(p.encode("utf-8")) <= 64 for p in payloads)


def test_with_two_buyers_each_row_names_its_buyer():
    """An alert can cover two buyers; an unlabelled second row would be a guess about whose state she set."""
    rows = formats.state_keyboard([(1, "Rosa Brás"), (2, "Raquel Gonçalves")])["inline_keyboard"]
    assert "Rosa" in rows[0][0]["text"] and "Raquel" in rows[1][0]["text"]
    assert all(b["callback_data"].startswith("m:1:") for b in rows[0])
    assert all(b["callback_data"].startswith("m:2:") for b in rows[1])


def test_the_current_state_is_marked_so_the_message_stays_true():
    rows = formats.state_keyboard([(1, "Rosa")], {1: "contactado"})["inline_keyboard"]
    marked = [b["text"] for b in rows[0] if b["text"].startswith("•")]
    assert len(marked) == 1 and "📞" in marked[0]


def test_the_buyer_choice_keyboard_always_offers_creating_a_new_one():
    keyboard = formats.buyer_choice_keyboard(5, [(7, "Ana Santos")])
    payloads = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
    assert "b:5:7" in payloads
    assert "b:5:0" in payloads      # 0 = "é um comprador novo"


# --- quiet hours --------------------------------------------------------------------------------

class _NoSettings:
    """A session with nothing stored, so the agreed defaults apply."""

    def get(self, *_args, **_kwargs):
        return None


@pytest.mark.parametrize("hour,expected", [(2, False), (7, False), (8, True), (20, True), (21, False), (23, False)])
def test_alerts_only_inside_the_hours_she_agreed(hour, expected):
    """08:00-21:00 Europe/Lisbon, agreed 2026-10-29."""
    now = datetime(2026, 10, 31, hour, 30, tzinfo=ZoneInfo("Europe/Lisbon"))
    assert appsettings.within_alert_hours(_NoSettings(), now) is expected


def test_quiet_hours_hold_alerts_rather_than_dropping_them():
    """`flush` returns 0 and leaves the rows pending, so a listing found at 02:00 still arrives at 08:00.
    Pinned here because silently discarding them would look identical in the logs."""
    import inspect

    from app.services import notifications

    source = inspect.getsource(notifications.flush)
    assert "within_alert_hours" in source
    assert "return 0" in source          # held, not marked sent


def test_the_defaults_are_her_agreed_hours_and_digest_time():
    assert appsettings.alert_hours(_NoSettings()) == (8, 21)
    assert appsettings.digest_at(_NoSettings()).strftime("%H:%M") == "08:30"


def test_a_corrupt_setting_falls_back_instead_of_crashing_the_flush():
    class _Bad:
        def get(self, _model, key):
            return SimpleNamespace(value={"from": "oito"}) if key == appsettings.ALERT_HOURS else None

    assert appsettings.alert_hours(_Bad()) == (8, 21)


# --- the digest ---------------------------------------------------------------------------------

def test_a_quiet_day_still_gets_a_digest_that_says_so():
    """Silence must be distinguishable from breakage."""
    text = formats.digest("31/10", [("Rosa Brás", 0, 5, 9), ("Raquel Gonçalves", 0, 1, 3)])
    assert "sem novidades" in text
    assert "Nada novo" in text


def test_the_digest_carries_the_agency_problems_that_are_no_longer_messaged_individually():
    text = formats.digest("31/10", [("Rosa", 1, 1, 0)], degraded_agencies=12)
    assert "12 mediadoras" in text


def test_one_degraded_agency_is_singular():
    assert "1 mediadora com" in formats.digest("31/10", [("Rosa", 0, 0, 0)], degraded_agencies=1)


# --- the allowlist: nothing reaches end clients (her requirement 4) -----------------------------

def _update(chat_id: int):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))


def test_only_her_chat_is_acted_on(monkeypatch):
    from app.bot import common

    monkeypatch.setattr(common, "configured_chat_id", lambda: "7660243159")
    assert common.allowed(_update(7660243159)) is True
    assert common.allowed(_update(123456789)) is False


def test_nothing_is_acted_on_before_a_chat_is_configured(monkeypatch):
    """An unconfigured bot must not treat every stranger as the operator."""
    from app.bot import common

    monkeypatch.setattr(common, "configured_chat_id", lambda: None)
    assert common.allowed(_update(123456789)) is False


def test_an_update_with_no_chat_is_ignored(monkeypatch):
    from app.bot import common

    monkeypatch.setattr(common, "configured_chat_id", lambda: "1")
    assert common.allowed(SimpleNamespace(effective_chat=None)) is False


def test_a_chat_id_is_compared_as_a_string_not_an_int():
    """`.env` gives a string, Telegram gives an int; comparing them raw silently locks her out."""
    import inspect

    from app.bot import common

    assert "str(" in inspect.getsource(common.allowed)
