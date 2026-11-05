"""The /dizer operator relay: Alex sends her a note from his own Telegram.

The thing worth protecting here is not the relay, it is the allowlist around it. Her requirement 4 was
explicit - nothing reaches end clients - so adding a second trusted chat is exactly the kind of change that
quietly turns one chat into two. These tests pin that the operator can relay and nothing else.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.bot import common
from app.bot.handlers import commands

HER = "8820204119"
ALEX = "7660243159"
STRANGER = "5555555555"


def update_from(chat_id: str | int) -> SimpleNamespace:
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))


# --- the decision itself, no database, no monkeypatching -------------------------------------------------

def test_operator_chat_is_recognised():
    assert common.chat_is_operator(update_from(ALEX), ALEX, HER) is True


def test_int_and_str_chat_ids_compare_equal():
    """Telegram gives an int and the environment gives a string; comparing them raw locks the operator out."""
    assert common.chat_is_operator(update_from(int(ALEX)), ALEX, HER) is True


def test_her_chat_is_not_the_operator():
    assert common.chat_is_operator(update_from(HER), ALEX, HER) is False


def test_a_stranger_is_not_the_operator():
    assert common.chat_is_operator(update_from(STRANGER), ALEX, HER) is False


def test_no_operator_configured_means_nobody_is_the_operator():
    for value in ("", None):
        assert common.chat_is_operator(update_from(ALEX), value, HER) is False


def test_operator_equal_to_her_chat_is_refused():
    """A misconfiguration that would relay her own message back to her, rather than echo it."""
    assert common.chat_is_operator(update_from(HER), HER, HER) is False


def test_update_without_a_chat_is_not_the_operator():
    assert common.chat_is_operator(SimpleNamespace(effective_chat=None), ALEX, HER) is False


# --- the allowlist must NOT have widened ----------------------------------------------------------------

def test_the_operator_is_not_on_the_buyer_allowlist():
    """`allowed()` is what guards every buyer-facing handler. The operator must fail it."""
    assert common.chat_matches(update_from(ALEX), HER) is False


# --- the relay ------------------------------------------------------------------------------------------

class Reply:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.texts.append(text)


def run_dizer(monkeypatch, *, args, sender=ALEX, chat=HER, send=None) -> Reply:
    reply = Reply()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=sender), message=reply)
    monkeypatch.setattr(commands, "is_operator", lambda u: _true(u, sender))
    monkeypatch.setattr(commands, "configured_chat_id", lambda: chat)
    monkeypatch.setattr(commands.telegram, "send_message", send or (lambda text, chat_id=None: 42))
    asyncio.run(commands.dizer(update, SimpleNamespace(args=args)))
    return reply


async def _true(update, sender):
    return str(update.effective_chat.id) == str(sender)


def test_a_note_is_relayed_and_confirmed(monkeypatch):
    sent = {}

    def send(text, chat_id=None):
        sent["text"], sent["chat"] = text, chat_id
        return 99

    reply = run_dizer(monkeypatch, args=["Bom", "dia"], send=send)
    assert sent["chat"] == HER
    assert sent["text"].startswith(commands.RELAY_PREFIX)
    assert sent["text"].endswith("Bom dia")
    assert "99" in reply.texts[0]


def test_html_is_escaped_by_default(monkeypatch):
    """parse_mode=HTML would reject this outright, so the message would simply never arrive."""
    sent = {}

    def send(text, chat_id=None):
        sent["text"] = text
        return 1

    run_dizer(monkeypatch, args=["preço", "<", "400k", "&", "garagem"], send=send)
    body = sent["text"][len(commands.RELAY_PREFIX):]
    assert body == "preço &lt; 400k &amp; garagem"


def test_html_flag_passes_markup_through_and_is_not_itself_sent(monkeypatch):
    sent = {}

    def send(text, chat_id=None):
        sent["text"] = text
        return 1

    run_dizer(monkeypatch, args=["--html", "<b>urgente</b>"], send=send)
    body = sent["text"][len(commands.RELAY_PREFIX):]
    assert body == "<b>urgente</b>"
    assert "--html" not in sent["text"]


def test_a_failure_is_reported_back_and_never_claimed_as_delivered(monkeypatch):
    def send(text, chat_id=None):
        raise RuntimeError("HTTP 400 can't parse entities")

    reply = run_dizer(monkeypatch, args=["ola"], send=send)
    assert "Não foi entregue" in reply.texts[0]
    assert "can't parse entities" in reply.texts[0]


def test_nothing_is_sent_for_a_stranger(monkeypatch):
    calls = []
    reply = run_dizer(monkeypatch, args=["ola"], sender=ALEX, send=lambda text, chat_id=None: calls.append(text))
    assert calls  # sanity: the operator path does send

    reply = Reply()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=STRANGER), message=reply)
    monkeypatch.setattr(commands, "is_operator", lambda u: _true(u, ALEX))
    monkeypatch.setattr(commands.telegram, "send_message",
                        lambda *a, **k: pytest.fail("a stranger must not be able to relay"))
    asyncio.run(commands.dizer(update, SimpleNamespace(args=["ola"])))
    assert reply.texts == []  # silent, so the bot's existence is not confirmed


def test_an_empty_command_shows_help_and_sends_nothing(monkeypatch):
    reply = run_dizer(monkeypatch, args=[],
                      send=lambda *a, **k: pytest.fail("an empty /dizer must send nothing"))
    assert "Canal do operador" in reply.texts[0]


def test_too_long_is_refused_before_sending(monkeypatch):
    reply = run_dizer(monkeypatch, args=["x" * (commands.RELAY_MAX_CHARS + 1)],
                      send=lambda *a, **k: pytest.fail("an over-long note must not be sent"))
    assert str(commands.RELAY_MAX_CHARS) in reply.texts[0]


def test_no_chat_id_yet_says_so_instead_of_failing(monkeypatch):
    reply = run_dizer(monkeypatch, args=["ola"], chat=None,
                      send=lambda *a, **k: pytest.fail("there is nowhere to deliver"))
    assert "Começar" in reply.texts[0]


def test_the_relay_does_not_touch_the_notification_queue():
    """A hand-typed note is not an alert: it must not be queued, retried, or move channel health.

    Walks the syntax tree rather than the source text. The same check written against the text tripped on
    the docstring that EXPLAINS the rule - the identical false positive the `smoke_day7` cap guard had.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(commands.dizer).lstrip())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in ("notifications", "enqueue", "record_send_result", "flush"):
        assert forbidden not in names, forbidden


def test_that_guard_would_catch_real_coupling():
    """Proves the tree walk still bites, so the previous test is not vacuously green."""
    import ast

    tree = ast.parse("def f():\n    notifications.enqueue(x)\n")
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "notifications" in names and "enqueue" in names
