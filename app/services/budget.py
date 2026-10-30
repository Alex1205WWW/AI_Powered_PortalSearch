"""The AI spend cap (deliverable 14): a warning, then a hard stop, and nothing else breaks.

The promise made to her was specific: a monthly ceiling, a warning before it is reached, and the AI
suspended when it is - while **crawling, matching and alerts carry on**. That separation is the whole point.
Reaching the cap must cost her the voice notes, not the search.

So the cap is enforced in exactly one place, `ai.client.budget_guard`, which every model call passes through,
and nowhere near `runs`, `properties`, `matches` or `notifications`. A suspended month looks like this:
portals still swept, listings still matched, Telegram still alerting, and the bot replying "não consigo
interpretar isto agora - use o formulário do painel", which needs no model at all.

The cap lives in `app_settings` so she can raise it from the settings page without a redeploy; the `.env`
value is only the starting default.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AiSpend
from app.services import appsettings

log = logging.getLogger(__name__)

CAP_KEY = "ai_cap_cents_month"
WARNED_KEY = "ai_cap_warned_month"
WARN_AT = 0.80

STATE_OK, STATE_WARNING, STATE_SUSPENDED = "ok", "warning", "suspended"


class BudgetExceeded(RuntimeError):
    """The monthly cap is reached. Callers fall back to the non-AI path; nothing else changes."""


def cap_cents(session: Session) -> int:
    """0 means no cap. The database wins over `.env` so she can change it herself."""
    stored = appsettings.get(session, CAP_KEY, None)
    try:
        return max(0, int(stored))
    except (TypeError, ValueError):
        return max(0, int(settings.ai_cap_cents_month or 0))


def set_cap_cents(session: Session, cents: int) -> None:
    appsettings.put(session, CAP_KEY, max(0, int(cents)))


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent_cents(session: Session, now: datetime | None = None) -> int:
    return int(session.scalar(
        select(func.coalesce(func.sum(AiSpend.cost_cents), 0)).where(AiSpend.created_at >= month_start(now))) or 0)


def status(session: Session, now: datetime | None = None) -> dict:
    """Everything the panel banner, the settings page and the digest need, in one read."""
    cap = cap_cents(session)
    spent = spent_cents(session, now)
    share = (spent / cap) if cap else 0.0
    state = STATE_OK
    if cap and spent >= cap:
        state = STATE_SUSPENDED
    elif cap and share >= WARN_AT:
        state = STATE_WARNING
    return {"cap_cents": cap, "spent_cents": spent, "share": share, "state": state,
            "remaining_cents": max(0, cap - spent) if cap else None}


def by_operation(session: Session, now: datetime | None = None) -> list[tuple[str, str | None, int, int]]:
    rows = session.execute(
        select(AiSpend.operation, AiSpend.model, func.count(AiSpend.id), func.coalesce(func.sum(AiSpend.cost_cents), 0))
        .where(AiSpend.created_at >= month_start(now))
        .group_by(AiSpend.operation, AiSpend.model).order_by(AiSpend.operation)).all()
    return [(r[0], r[1], int(r[2]), int(r[3])) for r in rows]


def ensure_within(session: Session, *, estimate_cents: int = 0, now: datetime | None = None) -> None:
    """Called before every model call. Raises `BudgetExceeded` at the cap; warns once per month at 80 %."""
    cap = cap_cents(session)
    if not cap:
        return
    spent = spent_cents(session, now)
    if spent + estimate_cents >= cap:
        log.warning("AI suspended: %d of %d cents spent this month", spent, cap)
        raise BudgetExceeded(f"o limite mensal de IA ({cap/100:.2f} USD) foi atingido")
    if spent / cap >= WARN_AT:
        _warn_once(session, spent, cap, now)


def _warn_once(session: Session, spent: int, cap: int, now: datetime | None) -> None:
    """One warning per calendar month, keyed on the month itself so a restart cannot re-send it."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    if str(appsettings.get(session, WARNED_KEY, "") or "") == stamp:
        return
    appsettings.put(session, WARNED_KEY, stamp)
    session.flush()
    text = (f"⚠️ <b>Gasto de IA a {spent / cap:.0%} do limite</b> deste mês "
            f"({spent/100:.2f} de {cap/100:.2f} USD).\n"
            f"Quando chegar ao limite, deixo de interpretar notas de voz — mas as pesquisas, "
            f"as comparações e os alertas <b>continuam a funcionar</b>. "
            f"Pode criar pesquisas no formulário do painel, que não usa IA.")
    try:
        from app.notify import telegram

        telegram.send_message(text, chat_id=appsettings.chat_id(session))
    except Exception as exc:  # noqa: BLE001 - telling her must never break the call that triggered it
        log.warning("budget warning not delivered: %r", exc)


def human(cents: int | None) -> str:
    return "—" if cents is None else f"{cents / 100:.2f} USD"
