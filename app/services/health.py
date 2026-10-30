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
from sqlalchemy.orm import Session

from app.db.models import Source

log = logging.getLogger(__name__)

FAILURES_BEFORE_DEGRADED = 2  # Roadmap_M1.md section 2 item 9


# Sources she is told about the moment they break. Agency sites are deliberately excluded: there are
# hundreds of them, and one platform-wide change (they mostly run the same eGO software) would degrade them
# together and send hundreds of Telegram messages. They are reported in the grouped panel banner and in the
# daily digest instead (Plan_Day5.md section 5 items 3-4).
ANNOUNCED_KINDS = ("portal", "email")


def _announce(source: Source, text: str) -> None:
    """Telling her must never break a crawl: a Telegram outage degrades the message, not the run."""
    from app.notify import telegram

    if source.kind not in ANNOUNCED_KINDS:
        log.info("source health changed, not announced individually", extra={"source": source.key})
        return
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
    rows = [{"name": s.name, "status": s.status, "error": s.last_error, "last_ok_at": s.last_ok_at}
            for s in unhealthy(session, ANNOUNCED_KINDS)]
    agencies = session.scalar(
        select(func.count(Source.id)).where(Source.enabled.is_(True), Source.kind == "agency",
                                            Source.status.in_(("degraded", "blocked")))) or 0
    if agencies:
        label = "mediadora" if agencies == 1 else "mediadoras"
        rows.append({"name": f"{agencies} {label} com problemas", "status": "degraded",
                     "error": "Ver a página de mediadoras para o detalhe.", "last_ok_at": None,
                     "agency_count": agencies})
    return rows
