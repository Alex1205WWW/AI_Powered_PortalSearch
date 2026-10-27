"""Telegram bot entrypoint (long polling). Day 1: /start only, so she can discover her chat id.
Voice intake, confirmation cards and state buttons arrive on Day 6. Run: python -m app.bot.main"""
from __future__ import annotations

import logging
import sys

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import settings
from app.logging_setup import setup_logging

log = logging.getLogger(__name__)


def allowed(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and settings.telegram_chat_id and str(chat.id) == str(settings.telegram_chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    if allowed(update):
        await update.message.reply_text("Ligado. Este chat recebe os alertas do painel.")
    else:
        # Not yet configured: tell the person their chat id so it can be put in .env. Nothing else is answered.
        await update.message.reply_text(f"O seu chat id é {chat.id}. Coloque-o em TELEGRAM_CHAT_ID para ativar.")
    log.info("start from chat %s (allowed=%s)", chat.id, allowed(update))


def main() -> None:
    setup_logging(settings.log_level)
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN not set; bot exits")
        sys.exit(0)
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", start))
    log.info("bot polling started")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
