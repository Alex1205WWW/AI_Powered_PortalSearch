"""Every inline button: confirm/correct/cancel a draft, pick a buyer, set a match state.

`callback_data` is the only state a button carries, so it is short and explicit:
    d:<draft_id>:ok|fix|no     the confirmation card
    b:<draft_id>:<buyer_id>    "that existing buyer" (0 = create a new one)
    m:<match_id>:<state>       a state button on an alert

Telegram re-delivers a callback until it is answered, so `answer()` is called on **every** path including
the error ones - otherwise the button spins forever on her phone and the update comes back again.
"""
from __future__ import annotations

import logging

from telegram import Update
from sqlalchemy import select
from telegram.ext import ContextTypes

from app.bot.common import guard, in_db
from app.db.models import Buyer, CriteriaDraft, PropertyMatch
from app.notify import formats
from app.services import drafts, matches

log = logging.getLogger(__name__)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not await guard(update):
        await query.answer()
        return
    kind, _, rest = (query.data or "").partition(":")
    try:
        if kind == "d":
            await _draft_action(query, rest)
        elif kind == "b":
            await _buyer_choice(query, rest)
        elif kind == "m":
            await _set_state(query, rest)
        else:
            await query.answer()
    except Exception as exc:  # noqa: BLE001 - never leave a button spinning
        log.exception("callback %s failed", query.data)
        await query.answer(f"Não consegui: {type(exc).__name__}", show_alert=True)


# --- the confirmation card ----------------------------------------------------------------------

async def _draft_action(query, rest: str) -> None:
    draft_id, _, action = rest.partition(":")
    draft_id = int(draft_id)
    if action == "no":
        await in_db(lambda s: drafts.cancel(s, s.get(CriteriaDraft, draft_id)))
        await query.answer("Cancelada.")
        await query.edit_message_text("Pesquisa cancelada.", reply_markup=None)
        return
    if action == "fix":
        await in_db(lambda s: _mark_awaiting(s, draft_id))
        await query.answer("Diga o que corrigir.")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "O que está errado? Escreva só a correção — por exemplo "
            "<i>“é Oeiras, não Lisboa”</i> ou <i>“o máximo é 450 mil”</i>.", parse_mode="HTML")
        return
    # confirm
    outcome = await in_db(lambda s: _confirm(s, draft_id))
    if outcome["kind"] == "ask_buyer":
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=formats.buyer_choice_keyboard(draft_id, outcome["candidates"]))
        await query.message.reply_text(outcome["text"], parse_mode="HTML")
        return
    if outcome["kind"] == "blocked":
        await query.answer(outcome["text"], show_alert=True)
        return
    await query.answer("Criada.")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(outcome["text"], parse_mode="HTML")


def _mark_awaiting(session, draft_id: int) -> None:
    draft = session.get(CriteriaDraft, draft_id)
    parsed = dict(draft.parsed or {})
    parsed["awaiting_correction"] = True
    draft.parsed = parsed


def _confirm(session, draft_id: int, buyer_id: int | None = None) -> dict:
    from app.services import digest

    draft = session.get(CriteriaDraft, draft_id)
    if draft is None or draft.status != drafts.STATUS_PENDING:
        return {"kind": "blocked", "text": "Esta pesquisa já não está pendente."}
    ok, why = drafts.ready(draft)
    if not ok:
        return {"kind": "blocked", "text": why}
    label = (draft.parsed or {}).get("buyer_label") or ""
    if buyer_id is None:
        exact, near = drafts.match_buyer(session, label)
        if exact is None and near:
            # Never create a second "Ana Santos" on a guess: a duplicate buyer splits her results in two.
            return {"kind": "ask_buyer",
                    "text": f"Já tenho compradores parecidos com <b>{label}</b>. É um destes?",
                    "candidates": [(b.id, b.label) for b in near]}
        buyer_id = exact.id if exact is not None else None
    criteria = drafts.confirm(session, draft, buyer_id=buyer_id)
    buyer = session.get(Buyer, criteria.buyer_id)
    return {"kind": "created", "text": formats.confirmed_text(buyer.label, digest.panel_url())}


async def _buyer_choice(query, rest: str) -> None:
    draft_id, _, buyer_id = rest.partition(":")
    chosen = int(buyer_id) or None
    outcome = await in_db(lambda s: _confirm(s, int(draft_id), buyer_id=chosen))
    if outcome["kind"] == "blocked":
        await query.answer(outcome["text"], show_alert=True)
        return
    await query.answer("Criada.")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(outcome["text"], parse_mode="HTML")


# --- state buttons on an alert ------------------------------------------------------------------

async def _set_state(query, rest: str) -> None:
    match_id, _, state = rest.partition(":")
    outcome = await in_db(lambda s: _apply_state(s, int(match_id), state))
    await query.answer(outcome["toast"])
    if outcome["keyboard"] is not None:
        # Re-render the row so the tapped state is visibly current, keeping the original text intact.
        await query.edit_message_reply_markup(reply_markup=outcome["keyboard"])


def _apply_state(session, match_id: int, state: str) -> dict:
    """Set the state, then rebuild the keyboard for every buyer on this property so the message stays true.

    One alert can cover several buyers, so the whole keyboard is re-rendered rather than just the row she
    tapped - otherwise the other rows would keep showing stale marks."""
    match = session.get(PropertyMatch, match_id)
    if match is None:
        return {"toast": "Esta correspondência já não existe.", "keyboard": None}
    matches.set_state(session, match_id, state)
    buyer = session.get(Buyer, match.buyer_id)
    siblings = list(session.scalars(
        select(PropertyMatch).where(PropertyMatch.property_id == match.property_id).order_by(PropertyMatch.id)))
    rows, current = [], {}
    for row in siblings:
        other = session.get(Buyer, row.buyer_id)
        rows.append((row.id, other.label if other else "?"))
        current[row.id] = row.state
    label = buyer.label if buyer else ""
    return {"toast": f"{label}: {formats.STATE_LABELS.get(state, state)}".strip(": "),
            "keyboard": formats.state_keyboard(rows or [(match_id, label or "?")], current)}
