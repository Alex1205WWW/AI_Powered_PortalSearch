"""Settings: the things she can change herself, without me and without a redeploy.

Deliberately short. Everything here is something she asked about or will want to adjust once the system is
in daily use - alert hours, the digest time, the AI ceiling - and everything else stays in `.env` where a
mistake cannot cost her anything.
"""
from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.services import appsettings, budget, health
from app.web.deps import DB, User, render

router = APIRouter(prefix="/settings")


@router.get("")
def show(request: Request, db: DB, user: User):
    start_hour, end_hour = appsettings.alert_hours(db)
    return render(request, "settings.html",
                  alert_from=start_hour, alert_to=end_hour,
                  digest_at=appsettings.digest_at(db).strftime("%H:%M"),
                  chat_id=appsettings.chat_id(db),
                  spend=budget.status(db),
                  spend_rows=budget.by_operation(db),
                  banner=health.banner(db),
                  money=budget.human)


@router.post("")
def save(request: Request, db: DB, user: User,
         alert_from: Annotated[int, Form()] = 8,
         alert_to: Annotated[int, Form()] = 21,
         digest_at: Annotated[str, Form()] = "08:30",
         cap_usd: Annotated[str, Form()] = "5.00"):
    messages = []
    start_hour, end_hour = max(0, min(23, alert_from)), max(1, min(24, alert_to))
    if start_hour >= end_hour:
        messages.append("A hora de início tem de ser anterior à de fim; mantive as anteriores.")
    else:
        appsettings.put(db, appsettings.ALERT_HOURS, {"from": start_hour, "to": end_hour})
        messages.append(f"Alertas das {start_hour:02d}:00 às {end_hour:02d}:00.")
    try:
        hours, _, minutes = digest_at.strip().partition(":")
        hour_value, minute_value = int(hours), int(minutes or 0)
        if not (0 <= hour_value <= 23 and 0 <= minute_value <= 59):
            raise ValueError
        appsettings.put(db, appsettings.DIGEST_AT, f"{hour_value:02d}:{minute_value:02d}")
        messages.append(f"Resumo diário às {hour_value:02d}:{minute_value:02d}.")
    except ValueError:
        messages.append("Hora do resumo inválida (use HH:MM); mantive a anterior.")
    try:
        cents = round(float(cap_usd.replace(",", ".")) * 100)
        budget.set_cap_cents(db, cents)
        messages.append(f"Limite de IA: {budget.human(cents)} por mês."
                        if cents else "Limite de IA removido (sem teto).")
    except ValueError:
        messages.append("Limite de IA inválido; mantive o anterior.")
    db.commit()
    return RedirectResponse(f"/settings?msg={quote(' '.join(messages))}", status_code=303)
