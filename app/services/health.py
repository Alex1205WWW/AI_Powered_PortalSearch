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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Source

log = logging.getLogger(__name__)

FAILURES_BEFORE_DEGRADED = 2  # Roadmap_M1.md section 2 item 9


def _announce(source: Source, text: str) -> None:
    """Telling her must never break a crawl: a Telegram outage degrades the message, not the run."""
    from app.notify import telegram

    try:
        telegram.send_message(text)
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


def unhealthy(session: Session) -> list[Source]:
    """Enabled sources the operator should know about, for the panel banner."""
    return list(session.scalars(
        select(Source).where(Source.enabled.is_(True), Source.status.in_(("degraded", "blocked")))
        .order_by(Source.name)))


def banner(session: Session) -> list[dict]:
    return [{"name": s.name, "status": s.status, "error": s.last_error,
             "last_ok_at": s.last_ok_at} for s in unhealthy(session)]
