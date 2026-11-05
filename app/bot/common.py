"""Shared bot plumbing: the allowlist, and how a handler touches the database.

Two things every handler needs and neither should reinvent.

**Database access runs in a worker thread.** SQLAlchemy sessions here are synchronous, and calling them
directly inside an async handler blocks the whole event loop - with long-polling that means the bot stops
answering while a sweep query runs. `in_db` hands the work to a thread and awaits it.

**The allowlist is one chat.** Her requirement 4 was explicit: nothing reaches end clients. So the bot
answers exactly one chat id and silently ignores every other, rather than replying "not allowed" to a
stranger (which would confirm the bot exists).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, TypeVar

from telegram import Update

from app.config import settings
from app.db.session import session_scope
from app.services import appsettings

log = logging.getLogger(__name__)
T = TypeVar("T")


async def in_db(work: Callable[..., T]) -> T:
    """Run `work(session)` in a thread, committing on success."""

    def runner() -> T:
        with session_scope() as session:
            result = work(session)
            session.commit()
            return result

    return await asyncio.to_thread(runner)


def configured_chat_id() -> str | None:
    with session_scope() as session:
        return appsettings.chat_id(session)


def _adopt_chat_id(chat_id: int) -> bool:
    """First /start claims the bot, when no chat is configured yet. -> True if adopted.

    Telegram forbids a bot from opening a conversation, so her chat id cannot be known before she writes to
    it. Rather than make her copy a number out of a message and wait for a redeploy, the first /start is
    adopted and stored. The bot token is the secret that protects this: whoever holds it could already send
    as the bot. Once set, it never changes by itself."""
    with session_scope() as session:
        if appsettings.chat_id(session):
            return False
        appsettings.put(session, appsettings.TELEGRAM_CHAT_ID, str(chat_id))
        session.commit()
    log.warning("chat id adopted from first /start: %s", chat_id)
    return True


async def adopt_chat_id(chat_id: int) -> bool:
    return await asyncio.to_thread(_adopt_chat_id, chat_id)


def chat_matches(update: Update, configured: str | None) -> bool:
    """The allowlist decision itself, with the stored chat id already in hand.

    Kept pure and separate from the lookup so the rule that protects her clients can be tested directly,
    with no database and no monkeypatching. `.env` gives a string and Telegram gives an int, so both sides
    are compared as strings: comparing them raw silently locks her out.
    """
    chat = update.effective_chat
    if chat is None:
        return False
    return bool(configured) and str(chat.id) == str(configured)


def chat_is_operator(update: Update, operator: str | None, buyer_chat: str | None) -> bool:
    """True when this update comes from Alex's own chat, used ONLY by /dizer.

    Deliberately a SEPARATE decision from `chat_matches`, not a widening of it. The allowlist exists to keep
    anything from reaching her end clients, and every buyer-facing handler must keep answering exactly one
    chat. The operator can relay a note to her and nothing else: he cannot create a search, press a state
    button or read a buyer.

    A misconfigured operator equal to her own chat would make /dizer relay her message back to herself, so
    that case is refused rather than producing a confusing echo.
    """
    chat = update.effective_chat
    if chat is None or not operator:
        return False
    if buyer_chat and str(operator) == str(buyer_chat):
        log.warning("operator chat id equals the buyer chat id; /dizer disabled")
        return False
    return str(chat.id) == str(operator)


async def is_operator(update: Update) -> bool:
    """The operator id comes from the environment, so only the buyer chat needs a database read."""
    configured = await asyncio.to_thread(configured_chat_id)
    return chat_is_operator(update, settings.telegram_operator_chat_id, configured)


async def allowed(update: Update) -> bool:
    """True when this update comes from the one chat the bot serves.

    Every update passes through here and the stored chat id is a database read, so it goes to a thread for
    the same reason `in_db` does: a synchronous session on the event loop stops the bot answering anything
    else until it returns.
    """
    return chat_matches(update, await asyncio.to_thread(configured_chat_id))


async def guard(update: Update) -> bool:
    """True when this update may be acted on. Unknown chats are ignored without a reply."""
    if await allowed(update):
        return True
    chat = update.effective_chat
    log.info("ignored update from chat %s", chat.id if chat else "?")
    return False
