"""`/findings` — the acceptance-test scoreboard.

The contract is "at least 80 % of the properties she finds manually", counting only properties published in
the test window and present in the agreed sources. That sentence has three conditions, and this page keeps
them separate instead of collapsing them into one flattering number:

  já tínhamos      the listing was in the panel. Not a crawl miss - if she never saw it, the gap was in the
                   alerting or in her reading the panel, and it still counts in our favour for the test.
  falha nossa      an agreed source had it and we did not. These are the only ones that count against us.
  fora do âmbito   a source that was declared out of scope in writing (Idealista direct, SUPERCASA, OLX,
                   Casafari). Excluded from the denominator, because including them would be scoring the
                   system against work it was never contracted to do.

Reported this way she can audit the score herself, which is the point of agreeing it in writing.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.db.models import Buyer, Listing, ManualFinding
from app.services import appsettings
from app.web.deps import DB, User, render

router = APIRouter(prefix="/findings")

LABELS = {"ja_tinha": "Já tínhamos", "fonte_acordada": "Falha nossa", "fora_do_ambito": "Fora do âmbito"}


@router.get("")
def show(request: Request, db: DB, user: User):
    window = appsettings.test_window(db)
    rows = []
    for finding in db.scalars(select(ManualFinding).order_by(ManualFinding.reported_at.desc()).limit(300)):
        listing = db.get(Listing, finding.matched_listing_id) if finding.matched_listing_id else None
        buyer = db.get(Buyer, finding.buyer_id) if finding.buyer_id else None
        rows.append({"finding": finding, "listing": listing, "buyer": buyer,
                     "label": LABELS.get(finding.verdict or "", finding.verdict or "—")})
    return render(request, "findings.html", rows=rows, window=window, labels=LABELS,
                  overall=score(rows), per_buyer=_per_buyer(rows))


def score(rows: list[dict]) -> dict:
    """The acceptance number, with its three exclusions kept visible instead of folded away.

    Out of scope by source, and published outside the window, are excluded by the contract. A finding we
    cannot date is **pending**, not excluded and not a failure: counting it either way would move the
    number that decides payment, so it is shown until someone settles it.
    """
    eligible, pending, excluded, had = 0, 0, 0, 0
    for row in rows:
        finding = row["finding"]
        if finding.verdict == "fora_do_ambito" or finding.published_in_window is False:
            excluded += 1
        elif finding.published_in_window is None:
            pending += 1
        else:
            eligible += 1
            had += finding.verdict == "ja_tinha"
    return {"had": had, "eligible": eligible, "pending": pending, "excluded": excluded,
            "pct": (had / eligible) if eligible else None}


def _per_buyer(rows: list[dict]) -> list[dict]:
    """The contract is two buyers, so one blended number can hide a buyer scoring far below 80 %."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["buyer"].label if row["buyer"] else "Sem comprador", []).append(row)
    return [{"name": name, **score(group)} for name, group in sorted(groups.items())]


@router.post("/{finding_id}/window")
def set_window(request: Request, db: DB, user: User, finding_id: int,
               inside: Annotated[str, Form()]):
    """Settle a pending finding by hand: was the advert published inside the test window?

    Only offered for the ones the system cannot date - the ones we never held, which is exactly the set
    the daily reconciliation in the plan asks a human to classify.
    """
    finding = db.get(ManualFinding, finding_id)
    if finding is not None:
        finding.published_in_window = inside == "sim"
        db.commit()
    return RedirectResponse("/findings", status_code=303)
