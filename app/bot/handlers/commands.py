"""Commands: /start, /estado, /pausar, /retomar, /ajuda.

Pause semantics matter more than they look. A paused buyer keeps accumulating matches in the panel and
sends nothing, and resuming sends nothing retroactively - `notifications.enqueue` refuses to queue for an
inactive buyer, so the paused window simply never becomes a backlog. That is what she asked for: clients on
hold must not be lost, but they must not buzz her phone either.
"""
from __future__ import annotations

import asyncio
import html
import logging

from sqlalchemy import func, select
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.common import adopt_chat_id, allowed, configured_chat_id, in_db, is_operator
from app.db.models import Buyer, Criteria, PropertyMatch
from app.notify import telegram
from app.services import appsettings, digest

log = logging.getLogger(__name__)

HELP = """<b>Como usar</b>

• Envie uma <b>nota de voz</b> (ou escreva) a descrever a pesquisa:
  <i>“Ana Santos, T1 ou T2 em Lisboa, 300 a 400 mil, com garagem”</i>
  Mostro-lhe o que entendi e só crio depois de confirmar.
• Nos alertas, use os botões para marcar: analisado, contactado, disponível, indisponível.
• <code>/estado</code> — como estão os compradores
• <code>/pausar Nome</code> e <code>/retomar Nome</code> — parar e retomar os alertas de um comprador
• <code>/faltou &lt;link&gt;</code> — um imóvel que encontrou e eu não trouxe
• A primeira busca de cada pesquisa aparece no <b>painel</b>, não aqui."""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if await allowed(update):
        await update.message.reply_text("Ligado. Este chat recebe os alertas.\n\n" + HELP, parse_mode="HTML")
        return
    if await adopt_chat_id(chat.id):
        await update.message.reply_text(
            "Pronto — este chat está agora ligado ao sistema e vai receber os alertas.\n\n" + HELP,
            parse_mode="HTML")
        return
    if await is_operator(update):
        await update.message.reply_text(OPERATOR_HELP, parse_mode="HTML")
        return
    # A chat id is already configured and it is not this one: say nothing useful.
    log.warning("start from unexpected chat %s", chat.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    await update.message.reply_text(HELP, parse_mode="HTML")


async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    text = await in_db(_status_text)
    await update.message.reply_text(text, parse_mode="HTML")


def _status_text(session) -> str:
    rows = []
    for buyer in session.scalars(select(Buyer).order_by(Buyer.label)):
        grades = dict(session.execute(
            select(PropertyMatch.grade, func.count(PropertyMatch.id))
            .where(PropertyMatch.buyer_id == buyer.id).group_by(PropertyMatch.grade)).all())
        searches = session.scalar(select(func.count(Criteria.id)).where(Criteria.buyer_id == buyer.id,
                                                                       Criteria.active.is_(True))) or 0
        mark = "▶️" if buyer.active else "⏸"
        rows.append(f"{mark} <b>{buyer.label}</b>: {grades.get('confirmada', 0)} confirmada(s) · "
                    f"{grades.get('possivel', 0)} possível(eis) · {searches} pesquisa(s)")
    if not rows:
        return "Ainda não há compradores."
    start_h, end_h = appsettings.alert_hours(session)
    rows.append("")
    rows.append(f"<i>Alertas das {start_h:02d}:00 às {end_h:02d}:00 · resumo às "
                f"{appsettings.digest_at(session).strftime('%H:%M')}</i>")
    url = digest.panel_url()
    if url:
        rows.append(f'<a href="{url}">Abrir o painel</a>')
    return "\n".join(rows)


async def pausar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_paused(update, context, paused=True)


async def retomar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_paused(update, context, paused=False)


async def _set_paused(update: Update, context: ContextTypes.DEFAULT_TYPE, *, paused: bool) -> None:
    if not await allowed(update):
        return
    name = " ".join(context.args or []).strip()
    if not name:
        verb = "pausar" if paused else "retomar"
        await update.message.reply_text(f"Diga de quem: <code>/{verb} Rosa Brás</code>", parse_mode="HTML")
        return
    text = await in_db(lambda s: _apply_pause(s, name, paused))
    await update.message.reply_text(text, parse_mode="HTML")


def _apply_pause(session, name: str, paused: bool) -> str:
    from app.services import buyers as buyer_svc
    from app.services.drafts import match_buyer

    exact, near = match_buyer(session, name)
    buyer = exact or (near[0] if len(near) == 1 else None)
    if buyer is None:
        if near:
            options = ", ".join(b.label for b in near[:5])
            return f"Qual deles? {options}"
        return f"Não encontrei o comprador “{name}”."
    buyer_svc.pause(session, buyer.id) if paused else buyer_svc.resume(session, buyer.id)
    if paused:
        return (f"⏸ <b>{buyer.label}</b> em pausa. Continuo a vigiar e a guardar tudo no painel, "
                f"mas não lhe mando alertas até retomar.")
    return f"▶️ <b>{buyer.label}</b> retomado. Só lhe mando o que aparecer de novo a partir de agora."


OPERATOR_HELP = """<b>Canal do operador</b>

Este chat não recebe alertas. Serve para lhe enviar um recado:

<code>/dizer o texto que quer que ela receba</code>

O texto chega ao Telegram dela com a indicação de que é um recado seu.
Para marcar texto use <code>/dizer --html &lt;b&gt;assim&lt;/b&gt;</code>."""

RELAY_PREFIX = "💬 <b>Recado do Alex</b>\n\n"
RELAY_MAX_CHARS = 3000


async def dizer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Relay one note from Alex's own Telegram into her chat. Operator only.

    Why a command and not plain text: a bare message in the operator chat would be relayed by accident the
    first time he typed a note to himself, and there is no unsend. `/dizer` makes every relay deliberate.

    The text is HTML-escaped unless he asks otherwise. `send_message` uses parse_mode=HTML, so an ordinary
    "preço < 400k" or "Rosa & Raquel" would come back HTTP 400 and simply never arrive - which is exactly
    the kind of sentence this command exists to send.

    It does not touch `notifications`: a hand-typed note is not an alert, so it must not enter the queue, be
    retried, or move the channel's health state. A failure is reported straight back to him instead.
    """
    if not await is_operator(update):
        return

    args = list(context.args or [])
    as_html = "--html" in args
    body = " ".join(a for a in args if a != "--html").strip()
    if not body:
        await update.message.reply_text(OPERATOR_HELP, parse_mode="HTML")
        return
    if len(body) > RELAY_MAX_CHARS:
        await update.message.reply_text(
            f"Demasiado longo: {len(body)} caracteres, o limite é {RELAY_MAX_CHARS}.")
        return

    chat = await asyncio.to_thread(configured_chat_id)
    if not chat:
        await update.message.reply_text(
            "Ela ainda não tocou em Começar, por isso não há para onde enviar.")
        return

    text = RELAY_PREFIX + (body if as_html else html.escape(body))
    try:
        message_id = await asyncio.to_thread(telegram.send_message, text, chat_id=chat)
    except Exception as error:  # noqa: BLE001 - he must see the reason, whatever it is
        log.warning("operator relay failed: %s", error)
        await update.message.reply_text(f"Não foi entregue: {error}")
        return
    log.info("operator relay delivered to %s as message %s", chat, message_id)
    await update.message.reply_text(f"Entregue (mensagem {message_id}).")
