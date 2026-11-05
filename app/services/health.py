"""Source health: the single place a source changes status, and the only place the operator is told.

Deliverable 15 ("aviso no painel quando uma fonte deixa de funcionar") needs two things that the failure
counters alone do not give: a banner she sees without looking for it, and one Telegram message when a source
stops working. The transition is the idempotency key — a source goes `ok -> degraded` once, so the alert
fires once no matter how many runs fail afterwards. No notification row is involved: `notifications` requires
a buyer, and this is an operator notice, not a buyer alert.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

from app.db.models import Source

log = logging.getLogger(__name__)

FAILURES_BEFORE_DEGRADED = 2  # Roadmap_M1.md section 2 item 9


# Sources she is told about the moment they break. Agency sites are deliberately excluded: there are
# hundreds of them, and one platform-wide change (they mostly run the same eGO software) would degrade them
# together and send hundreds of Telegram messages. They are reported in the grouped panel banner and in the
# daily digest instead (Plan_Day5.md section 5 items 3-4).
ANNOUNCED_KINDS = ("portal", "email")

# Shown in the panel banner. `channel` (the Telegram delivery channel) is here but NOT in
# ANNOUNCED_KINDS: when the channel itself is down, a Telegram message about it is the one notice that
# cannot be delivered, so the panel is the only honest place to say it.
BANNER_KINDS = ANNOUNCED_KINDS + ("channel", "worker")
CHANNEL_KEY = "telegram"


def _announce(source: Source, text: str) -> None:
    """Telling her must never break a crawl: a Telegram outage degrades the message, not the run.

    The chat id comes from the database, never from the environment. `TELEGRAM_CHAT_ID` in `.env` is
    deliberately empty since the handover, so that her first `/start` could be adopted - a send that
    falls back to it reaches nobody.
    """
    from app.notify import telegram
    from app.services import appsettings

    if source.kind not in ANNOUNCED_KINDS:
        log.info("source health changed, not announced individually", extra={"source": source.key})
        return
    try:
        # `object_session` raises on anything not mapped, and this whole block is swallowed - so an
        # unmapped caller would turn a delivery failure into silence. Ask only when it can answer.
        session = object_session(source) if isinstance(source, Source) else None
        telegram.send_message(text, chat_id=appsettings.chat_id(session) if session is not None else None)
    except Exception as exc:  # noqa: BLE001
        log.warning("source-health alert not delivered: %r", exc, extra={"source": source.key})


def mark_ok(source: Source) -> None:
    recovered = source.status == "degraded"
    source.consecutive_failures = 0
    source.last_ok_at = datetime.now(timezone.utc)
    source.last_error = None
    if recovered:
        source.status = "ok"
        _announce(source, f"✅ <b>{source.name}</b> voltou a funcionar.")
        log.info("source recovered", extra={"source": source.key})


def degrade(source: Source, reason: str) -> None:
    """Flip a source to `degraded` and announce it, but only on the edge."""
    source.last_error = reason[:2000]
    if source.status == "ok":
        source.status = "degraded"
        last_ok = source.last_ok_at.strftime("%d/%m %H:%M") if source.last_ok_at else "nunca"
        _announce(source, f"⚠️ <b>{source.name}</b> deixou de responder como esperado.\n"
                          f"Motivo: {reason[:300]}\nÚltima recolha correta: {last_ok}\n"
                          f"As outras fontes continuam a funcionar.")
        log.warning("source degraded: %s", reason, extra={"source": source.key})


def record_failure(source: Source, error: str) -> None:
    """A failed run. Two in a row is drift or an outage, not a blip."""
    source.consecutive_failures = (source.consecutive_failures or 0) + 1
    source.last_error = error[:2000]
    if source.consecutive_failures >= FAILURES_BEFORE_DEGRADED:
        degrade(source, error)


def unhealthy(session: Session, kinds: tuple[str, ...] | None = None) -> list[Source]:
    """Enabled sources the operator should know about, for the panel banner."""
    stmt = select(Source).where(Source.enabled.is_(True), Source.status.in_(("degraded", "blocked")))
    if kinds:
        stmt = stmt.where(Source.kind.in_(kinds))
    return list(session.scalars(stmt.order_by(Source.name)))


def banner(session: Session) -> list[dict]:
    """One row per broken portal, plus a single grouped row for the agency sites.

    Listing hundreds of agency rows individually would bury the one line that matters - that Imovirtual
    stopped responding - which is the opposite of what deliverable 15 is for."""
    rows = []
    for s in unhealthy(session, BANNER_KINDS):
        row = {"name": s.name, "status": s.status, "error": s.last_error, "last_ok_at": s.last_ok_at}
        if s.kind == "channel":
            # The default wording ("não está a responder. As outras fontes continuam a funcionar") would be
            # actively misleading here: nothing is wrong with the sources, the alerts are simply not
            # reaching her, and she needs to know how many are waiting and that the panel still has them.
            from app.services import notifications

            waiting = notifications.undelivered_count(session)
            if (s.last_error or "") == notifications.NO_TOKEN_REASON:
                # Nothing is broken on her side and there is nothing she can do: the bot has no token, so
                # not a single message can leave. Say what is true - the opportunities are still being
                # collected - and leave the fix named in the reason line, for whoever maintains it.
                row["text"] = ("o Telegram ainda não está configurado, por isso os alertas não saem. "
                               "As oportunidades continuam a ser recolhidas e estão no painel.")
                if waiting:
                    row["text"] += f" {waiting} à espera de envio."
                row["error"] = "Falta TELEGRAM_BOT_TOKEN no servidor (o bot está parado à espera dele)."
            elif (s.last_error or "") == notifications.NO_CHAT_REASON:
                # Not broken - never connected. Telling her "não estão a ser entregues" would read as a
                # fault in the system when the fix is one tap, so say the tap.
                row["text"] = ("falta ligar o Telegram. Abra o seu bot e toque em \"Começar\" — "
                               "a partir daí os alertas começam a chegar.")
                if waiting:
                    row["text"] += f" {waiting} já estão à espera."
                row["error"] = None
            else:
                row["text"] = "os alertas não estão a ser entregues. "
                if waiting:
                    row["text"] += f"{waiting} à espera de envio. "
                row["text"] += "As oportunidades continuam a ser recolhidas e estão no painel."
            # "Última recolha correta" is about crawling; nothing is being collected here. What the date
            # means for a channel is the last message that actually arrived.
            row["ok_label"] = "Último envio com sucesso"
        elif s.kind == "worker":
            # Her side of this is "nothing, I am on it". The detail is for whoever maintains the system,
            # so the reason line carries the job names and the headline does not alarm her.
            row["text"] = ("algumas tarefas automáticas não estão a correr como devem. "
                           "A recolha de imóveis continua e nada se perde — estou a ver o que se passa.")
            row["ok_label"] = "Última verificação correta"
        rows.append(row)
    counts = dict(session.execute(
        select(Source.status, func.count(Source.id))
        .where(Source.enabled.is_(True), Source.kind == "agency", Source.status.in_(("degraded", "blocked")))
        .group_by(Source.status)).all())
    degraded, blocked = int(counts.get("degraded", 0)), int(counts.get("blocked", 0))
    if degraded or blocked:
        # Say what is actually happening. "9 mediadoras com problemas não está a responder" read as nine
        # outages; a degraded agency site is being read, just with fewer fields than its profile promised,
        # and a blocked one is refused by the site's own robots.txt. Neither touches the portals.
        parts = []
        if degraded:
            parts.append(f"{degraded} com leitura incompleta" if degraded > 1 else "1 com leitura incompleta")
        if blocked:
            parts.append(f"{blocked} bloqueados pelo próprio site" if blocked > 1 else "1 bloqueado pelo próprio site")
        rows.append({"name": "Sites de mediadoras", "status": "degraded", "last_ok_at": None,
                     "text": " e ".join(parts) + ". Os anúncios já recolhidos continuam no painel.",
                     "error": "Ver a página Mediadoras para o detalhe por site.",
                     "agency_count": degraded + blocked})
    return rows
