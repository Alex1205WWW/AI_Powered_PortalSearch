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


def test_only_her_chat_is_acted_on():
    from app.bot import common

    assert common.chat_matches(_update(7660243159), "7660243159") is True
    assert common.chat_matches(_update(123456789), "7660243159") is False


def test_nothing_is_acted_on_before_a_chat_is_configured():
    """An unconfigured bot must not treat every stranger as the operator."""
    from app.bot import common

    assert common.chat_matches(_update(123456789), None) is False
    assert common.chat_matches(_update(123456789), "") is False


def test_an_update_with_no_chat_is_ignored():
    from app.bot import common

    assert common.chat_matches(SimpleNamespace(effective_chat=None), "1") is False


def test_a_chat_id_is_compared_as_a_string_not_an_int():
    """`.env` gives a string, Telegram gives an int; comparing them raw silently locks her out."""
    from app.bot import common

    assert common.chat_matches(_update(7660243159), "7660243159") is True
    assert common.chat_matches(SimpleNamespace(effective_chat=SimpleNamespace(id="7660243159")), 7660243159) is True


def test_the_allowlist_lookup_never_blocks_the_event_loop():
    """`allowed` reads the database on every single update. Doing that on the event loop is exactly what
    `in_db` exists to prevent, and it once made the bot stop answering while a sweep query ran."""
    import inspect

    from app.bot import common

    assert inspect.iscoroutinefunction(common.allowed)
    assert inspect.iscoroutinefunction(common.adopt_chat_id)
    assert "to_thread" in inspect.getsource(common.allowed)


# --- how the schema reaches the model (2026-11-04) ----------------------------------------------
# With her real key installed, `messages.parse(output_format=ParsedCriteria)` answered HTTP 400
# "Schema is too complex" and deliverable 3 was dead in production. No test could have caught it,
# because until that day there was no key to call with. These pin the mechanism that works.

class _StubMessages:
    def __init__(self, reply):
        self.reply, self.kwargs = reply, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.reply

    def parse(self, **kwargs):  # the call that failed in production
        raise AssertionError("parse_structured must not use messages.parse: 'Schema is too complex'")


class _StubClient:
    def __init__(self, reply):
        self.messages = _StubMessages(reply)


def _reply(tool_input, *, stop_reason="tool_use", name="registar_criterios"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name=name, input=tool_input)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1856, output_tokens=308,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )


class _LedgerSession:
    """Enough session for the budget guard and the spend ledger."""
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def scalar(self, _stmt):
        return 0

    def get(self, *_a, **_k):
        return None


def _patched(monkeypatch, reply):
    from app.ai import client as ai_client

    stub = _StubClient(reply)
    monkeypatch.setattr(ai_client, "anthropic_client", lambda: stub)
    monkeypatch.setattr(ai_client, "budget_guard", lambda _s: None)
    monkeypatch.setattr(ai_client, "record_llm_spend", lambda _s=None, **_k: None)
    return ai_client, stub


def test_the_schema_is_sent_as_a_tool_not_as_output_format(monkeypatch):
    """The whole point: `output_format` cannot carry this schema, a tool can."""
    ai_client, stub = _patched(monkeypatch, _reply({"buyer_label": "Ana Santos", "price_min": 300000}))
    out = ai_client.parse_structured(_LedgerSession(), operation="parse_criteria", system="s",
                                     user="Ana Santos 300 mil", output_format=ParsedCriteria)
    assert out.buyer_label == "Ana Santos" and out.price_min == 300000
    sent = stub.messages.kwargs
    assert "output_format" not in sent, "output_format is what produced the 400"
    assert sent["tool_choice"] == {"type": "tool", "name": ai_client.SCHEMA_TOOL_NAME}
    assert sent["tools"][0]["input_schema"] == ParsedCriteria.model_json_schema()
    assert sent["output_config"] == {"effort": "low"}, "the cost control must survive"


def test_the_full_criteria_schema_is_still_the_one_being_sent(monkeypatch):
    """Guards against 'fixing' the 400 by deleting fields she was promised."""
    ai_client, stub = _patched(monkeypatch, _reply({}))
    ai_client.parse_structured(_LedgerSession(), operation="parse_criteria", system="s", user="x",
                               output_format=ParsedCriteria)
    props = stub.messages.kwargs["tools"][0]["input_schema"]["properties"]
    for field in ("exclude", "nice_to_have", "ambiguities", "must_have", "zones", "estate_types"):
        assert field in props, f"{field} was dropped from the schema"
    assert sum(1 for p in props.values() if p.get("type") == "array") == 6


def test_a_malformed_tool_answer_becomes_airefused_so_the_panel_form_takes_over(monkeypatch):
    ai_client, _ = _patched(monkeypatch, _reply({"price_min": "trezentos mil"}))
    with pytest.raises(ai_client.AiRefused):
        ai_client.parse_structured(_LedgerSession(), operation="parse_criteria", system="s", user="x",
                                   output_format=ParsedCriteria)


def test_no_tool_block_and_a_refusal_both_become_airefused(monkeypatch):
    ai_client, _ = _patched(monkeypatch, SimpleNamespace(
        content=[SimpleNamespace(type="text", text="não")], stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0)))
    with pytest.raises(ai_client.AiRefused):
        ai_client.parse_structured(_LedgerSession(), operation="parse_criteria", system="s", user="x",
                                   output_format=ParsedCriteria)

    ai_client2, _ = _patched(monkeypatch, _reply({}, stop_reason="refusal"))
    with pytest.raises(ai_client2.AiRefused):
        ai_client2.parse_structured(_LedgerSession(), operation="parse_criteria", system="s", user="x",
                                    output_format=ParsedCriteria)


def test_a_billed_call_is_recorded_even_when_the_answer_is_unusable(monkeypatch):
    """It cost real money; her cap must see it."""
    from app.ai import client as ai_client

    stub = _StubClient(_reply({"price_min": "trezentos mil"}))
    recorded = []
    monkeypatch.setattr(ai_client, "anthropic_client", lambda: stub)
    monkeypatch.setattr(ai_client, "budget_guard", lambda _s: None)
    monkeypatch.setattr(ai_client, "record_llm_spend", lambda _s=None, **k: recorded.append(k))
    with pytest.raises(ai_client.AiRefused):
        ai_client.parse_structured(_LedgerSession(), operation="parse_criteria", system="s", user="x",
                                   output_format=ParsedCriteria)
    assert len(recorded) == 1 and recorded[0]["operation"] == "parse_criteria"


# --- a provider failure must reach her as words, not as silence (2026-11-04) ---------------------
# Her OpenAI key authenticates but has no credit (429 insufficient_quota). Raw, that exception escaped
# `on_voice`, `_on_error` only logged it, and she received NOTHING in reply to a voice note.

class _Quota(Exception):
    status_code = 429

    def __str__(self):
        return ("Error code: 429 - {'error': {'message': 'You have no credits remaining. Add credits to "
                "continue using the API at https://platform.openai.com/...', 'type': 'insufficient_quota', "
                "'code': 'credit_balance_exhausted'}}")


class _BadKey(Exception):
    status_code = 401

    def __str__(self):
        return "Error code: 401 - invalid_api_key"


@pytest.mark.parametrize("exc, expected", [
    (_Quota(), "sem saldo"),
    (_BadKey(), "recusada"),
])
def test_a_provider_failure_is_translated_into_a_reason_she_can_act_on(exc, expected):
    from app.ai import client as ai_client

    out = ai_client.as_unavailable(exc)
    assert isinstance(out, ai_client.AiUnavailable)
    assert expected in str(out), f"{expected!r} not in {str(out)!r}"


def test_transcription_quota_failure_becomes_aiunavailable_not_a_raw_sdk_error(monkeypatch):
    """`on_voice` catches only AiUnavailable and BudgetExceeded, so anything else is silence."""
    from app.ai import client as ai_client
    from app.ai import transcribe as tr

    class _Audio:
        class transcriptions:
            @staticmethod
            def create(**_k):
                raise _Quota()

    monkeypatch.setattr(ai_client, "budget_guard", lambda _s: None)
    monkeypatch.setattr(tr.ai, "budget_guard", lambda _s: None)
    monkeypatch.setattr(tr.ai, "openai_client", lambda: SimpleNamespace(audio=_Audio))

    with pytest.raises(ai_client.AiUnavailable) as caught:
        tr.transcribe(_LedgerSession(), b"x" * 10, duration_s=3)
    assert "sem saldo" in str(caught.value)


def test_an_llm_provider_failure_becomes_aiunavailable(monkeypatch):
    from app.ai import client as ai_client

    class _Msgs:
        @staticmethod
        def create(**_k):
            raise _Quota()

    monkeypatch.setattr(ai_client, "anthropic_client", lambda: SimpleNamespace(messages=_Msgs))
    monkeypatch.setattr(ai_client, "budget_guard", lambda _s: None)
    with pytest.raises(ai_client.AiUnavailable):
        ai_client.parse_structured(_LedgerSession(), operation="parse_criteria", system="s", user="x",
                                   output_format=ParsedCriteria)


def test_budget_and_guard_errors_are_not_swallowed_as_unavailable(monkeypatch):
    """A cap breach must stay a cap breach: she is told she hit her limit, not that the IA is down."""
    from app.ai import client as ai_client

    class _Msgs:
        @staticmethod
        def create(**_k):
            raise AssertionError("must not be called when the budget guard refuses")

    monkeypatch.setattr(ai_client, "anthropic_client", lambda: SimpleNamespace(messages=_Msgs))

    def refuse(_s):
        raise ai_client.BudgetExceeded("limite mensal atingido")

    monkeypatch.setattr(ai_client, "budget_guard", refuse)
    with pytest.raises(ai_client.BudgetExceeded):
        ai_client.parse_structured(_LedgerSession(), operation="parse_criteria", system="s", user="x",
                                   output_format=ParsedCriteria)


def test_the_bot_error_handler_replies_instead_of_only_logging():
    """Silence after a voice note reads as 'the system is broken' and is invisible as a bug report."""
    import asyncio

    from app.bot import main as bot_main

    sent = []

    class _Msg:
        async def reply_text(self, text, **_k):
            sent.append(text)

    asyncio.run(bot_main._on_error(SimpleNamespace(effective_message=_Msg()),
                                   SimpleNamespace(error=RuntimeError("boom"))))
    assert sent and "painel" in sent[0], "she must be told, and pointed somewhere that works"


def test_the_error_handler_survives_a_chat_that_cannot_be_replied_to():
    """The bot may itself be blocked; the handler must not raise from inside the error path."""
    import asyncio

    from app.bot import main as bot_main

    class _Blocked:
        async def reply_text(self, _text, **_k):
            raise RuntimeError("Forbidden: bot was blocked by the user")

    asyncio.run(bot_main._on_error(SimpleNamespace(effective_message=_Blocked()),
                                   SimpleNamespace(error=RuntimeError("boom"))))
    asyncio.run(bot_main._on_error(SimpleNamespace(effective_message=None),
                                   SimpleNamespace(error=RuntimeError("boom"))))


# --- delivery has to be observable and bounded (T1.1, 2026-11-04) --------------------------------
# A Telegram failure used to leave the rows untouched, log one line, and be retried every 2 minutes
# forever: 738 attempts in 24 h with the bot blocked, and nothing in the panel, the digest or the
# database saying her alerts were not arriving.

class _Row:
    def __init__(self, rid=1, attempts=0):
        self.id, self.buyer_id = rid, 1
        self.attempts, self.last_error, self.failed_at = attempts, None, None
        self.sent_at = None


class _ChannelSession:
    def __init__(self, channel):
        self.channel, self.flushed = channel, 0

    def scalar(self, _stmt):
        return self.channel

    def flush(self):
        self.flushed += 1


def _channel_source():
    return SimpleNamespace(key="telegram", name="Notificações (Telegram)", kind="channel",
                           status="ok", last_ok_at=None, last_error=None, consecutive_failures=0)


def test_a_failed_send_counts_an_attempt_on_every_row_of_the_group():
    from app.services import notifications

    rows = [_Row(1), _Row(2)]
    channel = _channel_source()
    notifications._record_delivery_failure(_ChannelSession(channel), rows, "HTTP 403 bot was blocked")
    assert [r.attempts for r in rows] == [1, 1]
    assert all("403" in (r.last_error or "") for r in rows)
    assert all(r.failed_at is None for r in rows), "one failure is not a give-up"


def test_the_channel_degrades_so_the_panel_can_say_so():
    """Two failures is the project's existing threshold for 'not a blip'."""
    from app.services import health, notifications

    channel = _channel_source()
    session = _ChannelSession(channel)
    for _ in range(health.FAILURES_BEFORE_DEGRADED):
        notifications._record_delivery_failure(session, [_Row()], "HTTP 403 bot was blocked")
    assert channel.status == "degraded"
    assert "403" in (channel.last_error or "")


def test_the_channel_is_never_announced_over_the_channel_that_is_down():
    from app.services import health

    assert "channel" in health.BANNER_KINDS, "she must see it in the panel"
    assert "channel" not in health.ANNOUNCED_KINDS, "a Telegram outage cannot announce itself by Telegram"


def test_an_alert_is_given_up_on_at_the_cap_so_it_cannot_block_the_queue():
    """`pending()` is ordered by created_at, so one undeliverable row left in the queue would hold up
    every alert behind it."""
    from app.services import notifications

    row = _Row(attempts=notifications.MAX_ATTEMPTS - 1)
    notifications._record_delivery_failure(_ChannelSession(_channel_source()), [row], "chat not found")
    assert row.attempts == notifications.MAX_ATTEMPTS
    assert row.failed_at is not None, "past the cap the row must leave the queue"


def test_the_pending_queue_excludes_rows_given_up_on():
    """Structural, so it cannot drift: the filter must be in the query itself."""
    import inspect

    from app.services import notifications

    source = inspect.getsource(notifications.pending)
    assert "failed_at.is_(None)" in source, "a given-up row would be retried forever"
    assert "sent_at.is_(None)" in source


def test_a_successful_send_clears_the_channel_state():
    from app.services import notifications

    channel = _channel_source()
    channel.status, channel.consecutive_failures = "degraded", 4
    notifications._record_delivery_ok(_ChannelSession(channel))
    assert channel.status == "ok" and channel.consecutive_failures == 0


def test_the_banner_tells_her_alerts_are_undelivered_not_that_a_source_is_down(monkeypatch):
    """base.html appends "não está a responder. As outras fontes continuam a funcionar" unless the row
    carries its own sentence - which for the channel would be plainly wrong: the sources are fine."""
    from app.services import health, notifications

    monkeypatch.setattr(health, "unhealthy", lambda session, kinds=None: [
        SimpleNamespace(name="Notificações (Telegram)", kind="channel", status="degraded",
                        last_error="HTTP 403 bot was blocked by the user", last_ok_at=None)])
    monkeypatch.setattr(notifications, "undelivered_count", lambda _s: 17)

    class FakeResult:
        def all(self): return []

    class FakeSession:
        def execute(self, _stmt): return FakeResult()

    rows = health.banner(FakeSession())
    assert len(rows) == 1
    text = rows[0]["text"]
    assert "não estão a ser entregues" in text
    assert "17" in text, "she needs the number waiting"
    assert "no painel" in text, "and that the opportunities are not lost"
    assert "outras fontes" not in text


def test_the_undelivered_count_is_every_stuck_alert_not_only_the_tried_ones():
    """`flush()` breaks on the first failure, so with a dead channel exactly one group carries attempts.
    Counting attempts reported "1 à espera" on production while 197 alerts were going nowhere."""
    import inspect

    from app.services import notifications

    source = inspect.getsource(notifications.undelivered_count)
    body = source.replace(notifications.undelivered_count.__doc__ or "", "")  # prose mentions attempts
    assert "Notification.attempts" not in body, "counting attempts under-reports by the whole queue"
    assert "sent_at.is_(None)" in body
    assert "ALERT_REASONS" in body, "only alert reasons; the digest is not an alert"
    # The banner says "à espera de envio", so a retired or given-up row must not be counted: it is not
    # waiting for anything. 197 rows were retired at handover rather than delivered in one burst.
    assert "failed_at.is_(None)" in body, "a retired alert would claim to be waiting for ever"


def test_the_channel_row_relabels_the_date_it_shows(monkeypatch):
    """base.html says "Última recolha correta", which is about crawling. Nothing is collected from a
    delivery channel; the date that matters is the last message that actually arrived."""
    from app.services import health, notifications

    monkeypatch.setattr(health, "unhealthy", lambda session, kinds=None: [
        SimpleNamespace(name="Notificações (Telegram)", kind="channel", status="degraded",
                        last_error="HTTP 403", last_ok_at=None)])
    monkeypatch.setattr(notifications, "undelivered_count", lambda _s: 0)

    class FakeResult:
        def all(self): return []

    class FakeSession:
        def execute(self, _stmt): return FakeResult()

    row = health.banner(FakeSession())[0]
    assert row["ok_label"] == "Último envio com sucesso"
    assert "à espera de envio" not in row["text"], "do not print a count of zero"


def test_a_portal_row_keeps_the_default_label(monkeypatch):
    from app.services import health

    monkeypatch.setattr(health, "unhealthy", lambda session, kinds=None: [
        SimpleNamespace(name="Imovirtual", kind="portal", status="degraded",
                        last_error="canary", last_ok_at=None)])

    class FakeResult:
        def all(self): return []

    class FakeSession:
        def execute(self, _stmt): return FakeResult()

    row = health.banner(FakeSession())[0]
    assert "ok_label" not in row and "text" not in row, "portals use base.html's own wording"
