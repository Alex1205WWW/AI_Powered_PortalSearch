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


def build_application() -> Application:
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", commands.start))
    application.add_handler(CommandHandler(["ajuda", "help"], commands.help_command))
    application.add_handler(CommandHandler("estado", commands.estado))
    application.add_handler(CommandHandler("pausar", commands.pausar))
    application.add_handler(CommandHandler("retomar", commands.retomar))
    application.add_handler(CommandHandler("faltou", findings.faltou))
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


def main() -> None:
    setup_logging(settings.log_level)
    if not settings.telegram_bot_token:
        # Under compose `restart: unless-stopped` an exit would loop every few seconds; idling keeps the
        # container quiet and "Up" until the token is configured.
        log.warning("TELEGRAM_BOT_TOKEN not set; bot idle until it is configured (restart the container afterwards)")
        while True:
            time.sleep(3600)
            log.warning("bot idle: TELEGRAM_BOT_TOKEN still not set")
    application = build_application()
    log.info("bot polling started")
    # `drop_pending_updates` avoids replaying a backlog of taps after a long outage: acting on a button
    # pressed hours ago, against data that has since changed, is worse than ignoring it.
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
