"""Telegram bot entrypoint (long polling). Run: python -m app.bot.main

Long polling rather than a webhook: no inbound port, no TLS plumbing, and it keeps working if the DuckDNS
name or the certificate ever lapses. One chat only - hers.

Nothing conversational is held in memory. A draft is a database row, so a restart in the middle of
"Confirma esta pesquisa?" loses nothing: the card's buttons still carry the draft id and still work.
"""
from __future__ import annotations

import logging
import time

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from app.bot.handlers import callbacks, commands, findings, intake
from app.config import settings
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)

# How often the idle loop re-asserts "there is still no token" on the channel row.
IDLE_REPORT_S = 3600


def build_application() -> Application:
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", commands.start))
    application.add_handler(CommandHandler(["ajuda", "help"], commands.help_command))
    application.add_handler(CommandHandler("estado", commands.estado))
    application.add_handler(CommandHandler("pausar", commands.pausar))
    application.add_handler(CommandHandler("retomar", commands.retomar))
    application.add_handler(CommandHandler("faltou", findings.faltou))
    # Operator-only: Alex relays a note to her from his own Telegram. Never reaches a buyer handler.
    application.add_handler(CommandHandler("dizer", commands.dizer))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, intake.on_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, intake.on_text))
    application.add_handler(CallbackQueryHandler(callbacks.on_callback))
    application.add_error_handler(_on_error)
    return application


async def _on_error(update: object, context) -> None:
    """A failing handler must never stop the polling loop, and must never leave her without an answer.

    Logging alone was not enough. Any exception a handler does not catch used to end there, so she would
    send a voice note and receive nothing at all - no error, no hint, just silence - which reads as "the
    system is broken" and is the worst possible behaviour during the acceptance test. One sentence back is
    the difference between a bug she can report and a bug she cannot see.
    """
    log.exception("bot handler error", exc_info=context.error)
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    try:
        await message.reply_text(
            "Não consegui processar esse pedido. O erro ficou registado.\n"
            "Pode usar o formulário no painel, que funciona sempre."
        )
    except Exception:  # noqa: BLE001 - the reply itself may fail (blocked bot); never recurse
        log.warning("could not deliver the error notice to the chat")


def _report_token_state(missing: bool) -> None:
    """Put the token state where someone will see it: the panel banner, via the channel source row.

    Idling with a log line was the worst of the three options. The container reads "Up", the panel reads
    `ok`, and nothing at all is delivered - the failure mode that looks healthiest from the outside. The
    database is the only channel the bot shares with the panel, so that is where it says so.

    Never fatal: a database that is briefly unreachable must not stop the bot from starting, or from
    idling quietly while it waits for the token.
    """
    from app.db.session import session_scope
    from app.services import notifications

    try:
        with session_scope() as session:
            if missing:
                notifications.mark_channel_token_missing(session)
            else:
                notifications.clear_channel_token_missing(session)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - reporting a fault must never become one
        log.warning("could not record the Telegram token state: %r", exc)


def main() -> None:
    setup_logging(settings.log_level)
    if not settings.telegram_bot_token:
        # Under compose `restart: unless-stopped` an exit would loop every few seconds; idling keeps the
        # container quiet until the token is configured. The panel is what reports it, not the log.
        log.error("TELEGRAM_BOT_TOKEN not set; bot idle until it is configured (restart the container afterwards)")
        while True:
            # Re-asserted on every tick, not only at startup: the channel row is shared state and a
            # successful send elsewhere, or someone clearing the banner by hand, would otherwise leave the
            # panel claiming delivery works while this container still has no token.
            _report_token_state(missing=True)
            time.sleep(IDLE_REPORT_S)
    # A token exists now, so the stored "no token" fault has no cause left. Only that one is cleared: a
    # channel degraded by a real delivery failure stays degraded until a send succeeds.
    _report_token_state(missing=False)
    application = build_application()
    log.info("bot polling started")
    # `drop_pending_updates` avoids replaying a backlog of taps after a long outage: acting on a button
    # pressed hours ago, against data that has since changed, is worse than ignoring it.
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
