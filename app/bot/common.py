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


def adopt_chat_id(chat_id: int) -> bool:
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


def allowed(update: Update) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    configured = configured_chat_id()
    return bool(configured) and str(chat.id) == str(configured)


async def guard(update: Update) -> bool:
    """True when this update may be acted on. Unknown chats are ignored without a reply."""
    if allowed(update):
        return True
    chat = update.effective_chat
    log.info("ignored update from chat %s", chat.id if chat else "?")
    return False
