"""Voice notes and text become a draft search, shown back for confirmation.

The flow she was promised: she dictates, the system says what it understood, she confirms. Nothing is
created until she taps Confirmar, so a mis-heard price never becomes a live search.

When the AI is unavailable (no key, monthly cap reached, model refusal) the bot says so plainly and points
her at the panel form. The structured form never needs a model, so "the AI is off" is never "the system is
down" - which is exactly what the Day 7 spend cap depends on.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.ai import client as ai
from app.ai import parse_criteria, transcribe
from app.bot.common import guard, in_db
from app.db.models import Geo
from app.notify import formats
from app.services import drafts

log = logging.getLogger(__name__)

FALLBACK = ("Não consegui interpretar isto automaticamente neste momento ({why}).\n"
            "Pode criar a pesquisa no painel, no formulário — funciona sempre, sem IA.")
TOO_SHORT = "Diga-me um pouco mais: o comprador, a tipologia, a zona e o orçamento."


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    voice = update.message.voice or update.message.audio
    if voice is None:
        return
    await update.message.chat.send_action("typing")
    telegram_file = await context.bot.get_file(voice.file_id)
    audio = bytes(await telegram_file.download_as_bytearray())
    try:
        text = await in_db(lambda s: transcribe.transcribe(s, audio, duration_s=getattr(voice, "duration", None)))
    except transcribe.AudioTooLong:
        await update.message.reply_text("A nota de voz é demasiado longa. Envie uma mais curta, por favor.")
        return
    except (ai.AiUnavailable, ai.BudgetExceeded) as exc:
        await update.message.reply_text(FALLBACK.format(why=exc), parse_mode="HTML")
        return
    if not text:
        await update.message.reply_text("Não percebi nada na gravação. Pode repetir?")
        return
    await update.message.reply_text(f"Ouvi: <i>{text}</i>", parse_mode="HTML")
    await _build_draft(update, text, file_id=voice.file_id)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free text, and also the correction she types after tapping Corrigir."""
    if not await guard(update):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    if len(text) < 12:
        await update.message.reply_text(TOO_SHORT)
        return
    chat_id = update.effective_chat.id
    pending = await in_db(lambda s: _pending_id(s, chat_id))
    if pending is not None:
        await _apply_correction(update, pending, text)
        return
    await _build_draft(update, text)


def _pending_id(session, chat_id: int) -> int | None:
    """Only a draft that is actually waiting for a correction swallows the next message.

    A pending draft she never answered must not capture an unrelated note hours later, so the flag set by
    the Corrigir button is what gates this - not merely the draft's existence."""
    draft = drafts.pending_for_chat(session, chat_id)
    if draft is None:
        return None
    return draft.id if (draft.parsed or {}).get("awaiting_correction") else None


async def _apply_correction(update: Update, draft_id: int, correction: str) -> None:
    await update.message.chat.send_action("typing")
    try:
        payload = await in_db(lambda s: _correct(s, draft_id, correction))
    except (ai.AiUnavailable, ai.BudgetExceeded, ai.AiRefused) as exc:
        await update.message.reply_text(FALLBACK.format(why=exc), parse_mode="HTML")
        return
    await update.message.reply_text(payload["card"], parse_mode="HTML",
                                    reply_markup=formats.draft_keyboard(draft_id))


def _correct(session, draft_id: int, correction: str) -> dict:
    from app.db.models import CriteriaDraft

    draft = session.get(CriteriaDraft, draft_id)
    drafts.apply_correction(session, draft, correction)
    parsed = dict(draft.parsed or {})
    parsed["awaiting_correction"] = False
    draft.parsed = parsed
    return {"card": formats.criteria_card(parsed, draft.ambiguities or [], _zone_names(session, parsed))}


async def _build_draft(update: Update, text: str, file_id: str | None = None) -> None:
    chat_id = update.effective_chat.id
    await update.message.chat.send_action("typing")
    try:
        payload = await in_db(lambda s: _make(s, text, chat_id, file_id))
    except (ai.AiUnavailable, ai.BudgetExceeded, ai.AiRefused) as exc:
        await update.message.reply_text(FALLBACK.format(why=exc), parse_mode="HTML")
        return
    except Exception as exc:  # noqa: BLE001 - a bad note must never kill the bot
        log.exception("draft build failed")
        await update.message.reply_text(FALLBACK.format(why=type(exc).__name__), parse_mode="HTML")
        return
    await update.message.reply_text(payload["card"], parse_mode="HTML",
                                    reply_markup=formats.draft_keyboard(payload["draft_id"]))


def _make(session, text: str, chat_id: int, file_id: str | None) -> dict:
    parsed, notes = parse_criteria.parse(session, text)
    draft = drafts.create(session, transcript=text, parsed=parsed, notes=notes,
                          chat_id=chat_id, file_id=file_id)
    return {"draft_id": draft.id,
            "card": formats.criteria_card(draft.parsed, draft.ambiguities or [],
                                          _zone_names(session, draft.parsed))}


def _zone_names(session, parsed: dict) -> list[str]:
    names = []
    for code in parsed.get("zone_codes") or []:
        geo = session.get(Geo, code)
        names.append(geo.name if geo else code)
    return names
