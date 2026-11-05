"""Send one message by hand to her Telegram, from the operator's seat.

Usage (inside the worker container, from /srv/imoveis):

    docker compose exec -T worker python -m scripts.say "texto aqui"
    docker compose exec -T worker python -m scripts.say --dry "texto aqui"
    echo "varias
linhas" | docker compose exec -T worker python -m scripts.say -

Why this exists: she reads Telegram, not the panel, and there is no other way to say something to her
from the system. It deliberately does NOT touch `notifications` - a manual note is not an alert, so it
must not appear in the queue, must not be retried, and must not move the channel's health state. If it
fails you see the exception here and decide what to do, which is the right behaviour for a human-typed
one-off.

Two safety choices worth keeping:

* The text is HTML-ESCAPED by default. `send_message` uses parse_mode=HTML, so an innocent "preço < 400k"
  or "Rosa & Raquel" would be rejected by Telegram with HTTP 400 and the message would simply not arrive.
  Pass `--html` when you really are writing tags (<b>, <i>, <a href>).
* The chat id comes from the database (`app_settings`), never from `.env`. `.env` is only a fallback inside
  `send_message`, and during the handover it was deliberately emptied - reading it here could send to the
  wrong chat or to nowhere. The id shown before sending is the one that will be used.
"""
from __future__ import annotations

import html
import sys

from app.db.session import session_scope
from app.notify import telegram
from app.services import appsettings

args = [a for a in sys.argv[1:]]
as_html = "--html" in args
dry = "--dry" in args
body = " ".join(a for a in args if a not in {"--html", "--dry"}).strip()

if body == "-" or not body:
    body = sys.stdin.read().strip()

if not body:
    sys.exit("nada para enviar: passe o texto como argumento ou por stdin")

text = body if as_html else html.escape(body)

with session_scope() as session:
    chat = appsettings.chat_id(session)

if not chat:
    sys.exit("sem chat id configurado: ela ainda nao tocou em Start, nao ha para onde enviar")

print(f"chat: {chat}  ({len(text)} chars, parse_mode=HTML, escaped={not as_html})")
print("---")
print(text)
print("---")

if dry:
    print("DRY RUN - nada foi enviado")
    raise SystemExit(0)

message_id = telegram.send_message(text, chat_id=chat)
print(f"enviado, message_id={message_id}")
