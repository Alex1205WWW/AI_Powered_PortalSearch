"""Send-only Telegram client (Bot API over HTTPS). Used by worker and CLI; the interactive bot lives in app.bot.
Everything goes to one chat (hers). Messages are spaced to stay far below Telegram's per-chat limit."""
from __future__ import annotations

import logging
import threading
import time

import httpx

from app.config import settings

log = logging.getLogger(__name__)
_lock = threading.Lock()
_last_sent = 0.0


class TelegramError(RuntimeError):
    pass


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


def _space() -> None:
    global _last_sent
    with _lock:
        wait = settings.telegram_min_spacing_s - (time.monotonic() - _last_sent)
        if wait > 0:
            time.sleep(wait)
        _last_sent = time.monotonic()


def _call(method: str, payload: dict) -> dict:
    if not settings.telegram_bot_token:
        # Raise, never return empty. A caller that cannot tell "sent" from "silently skipped" will record
        # the message as delivered - see the note on `send_message`.
        raise TelegramError(f"{method} not attempted: TELEGRAM_BOT_TOKEN is not configured")
    _space()
    with httpx.Client(timeout=20.0) as client:
        response = client.post(_api(method), json=payload)
    data = response.json() if response.content else {}
    if response.status_code != 200 or not data.get("ok"):
        raise TelegramError(f"{method} failed: HTTP {response.status_code} {data.get('description')}")
    return data["result"]


def send_message(text: str, *, chat_id: str | int | None = None, reply_markup: dict | None = None, disable_preview: bool = True) -> int | None:
    """-> the Telegram message id. Raises `TelegramError` if the message could not be sent, for ANY reason.

    [!] This used to log a warning and return None when no chat id was configured, and that destroyed 96 of
    her alerts on 2026-11-04. `notifications.flush()` cannot distinguish a `None` return from a successful
    send, so it stamped `sent_at` on every one of them: marked delivered, never sent, and dropped out of the
    pending queue for good. The channel health even stayed `ok`, so the panel reported everything fine.

    The window was opened by clearing `TELEGRAM_CHAT_ID` during the handover so her first `/start` could be
    adopted - a deliberate change that turned a safe "queue until the bot works" state into silent data
    loss. Never signal failure by a return value here: the only honest options are a message id or an
    exception. Callers that must not break on a Telegram outage (`health.py`, `budget.py`) already catch it.
    """
    chat = chat_id or settings.telegram_chat_id
    if not chat:
        raise TelegramError("sendMessage not attempted: no chat id configured "
                            "(she has not pressed Start yet, so there is nowhere to deliver)")
    payload = {"chat_id": chat, "text": text[:4096], "parse_mode": "HTML", "link_preview_options": {"is_disabled": disable_preview}}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = _call("sendMessage", payload)
    return result.get("message_id") if result else None


def edit_message(message_id: int, text: str, *, chat_id: str | int | None = None, reply_markup: dict | None = None) -> None:
    chat = chat_id or settings.telegram_chat_id
    payload = {"chat_id": chat, "message_id": message_id, "text": text[:4096], "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _call("editMessageText", payload)


def get_me() -> dict:
    return _call("getMe", {})
