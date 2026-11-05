"""No caller of `telegram.send_message` may rely on the environment for the chat id.

`TELEGRAM_CHAT_ID` in `.env` is deliberately EMPTY since the 2026-11-04 handover, so that her first
`/start` could be adopted; the real id lives in `app_settings`. A send that omits `chat_id` therefore
falls back to an empty value and reaches nobody.

This was found twice. The first time (`notifications.flush`) the transport returned None on failure and
96 alerts were marked delivered without ever being sent. The fix made it raise - which then turned the
two remaining env-reliant callers (`health._announce`, `cli.cmd_send_test`) from silently-nobody into
loudly-broken. `_announce` swallows exceptions by design, so the Telegram half of deliverable 15 was
dead with nothing in the logs to say so.

A per-call-site assertion is the only thing that catches this: every individual fix passed its own test.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"


def _sends_without_chat_id() -> list[str]:
    offenders = []
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "send_message":
                continue
            if not any(kw.arg == "chat_id" for kw in node.keywords):
                offenders.append(f"{path.relative_to(ROOT.parent).as_posix()}:{node.lineno}")
    return offenders


def test_no_caller_omits_the_chat_id():
    offenders = _sends_without_chat_id()
    assert not offenders, (
        "these send to whatever TELEGRAM_CHAT_ID happens to hold, which is empty in production: "
        + ", ".join(offenders))


def test_the_check_can_actually_fail(tmp_path, monkeypatch):
    # A structural test that cannot fail is decoration. Prove it sees a bad call.
    bad = tmp_path / "app" / "x.py"
    bad.parent.mkdir()
    bad.write_text("telegram.send_message('oi')\n", encoding="utf-8")
    monkeypatch.setattr("tests.test_every_send_names_the_chat.ROOT", bad.parent)
    assert _sends_without_chat_id() == ["app/x.py:1"]
