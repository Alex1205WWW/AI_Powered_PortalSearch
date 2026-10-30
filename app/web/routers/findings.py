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

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.db.models import Buyer, Listing, ManualFinding
from app.web.deps import DB, User, render

router = APIRouter(prefix="/findings")

LABELS = {"ja_tinha": "Já tínhamos", "fonte_acordada": "Falha nossa", "fora_do_ambito": "Fora do âmbito"}


@router.get("")
async def show(request: Request, db: DB, user: User):
    rows = []
    for finding in db.scalars(select(ManualFinding).order_by(ManualFinding.reported_at.desc()).limit(300)):
        listing = db.get(Listing, finding.matched_listing_id) if finding.matched_listing_id else None
        buyer = db.get(Buyer, finding.buyer_id) if finding.buyer_id else None
        rows.append({"finding": finding, "listing": listing, "buyer": buyer,
                     "label": LABELS.get(finding.verdict or "", finding.verdict or "—")})
    counted = [r for r in rows if r["finding"].verdict in ("ja_tinha", "fonte_acordada")]
    had = sum(1 for r in counted if r["finding"].verdict == "ja_tinha")
    score = (had / len(counted)) if counted else None
    return render(request, "findings.html", rows=rows, score=score, had=had,
                  counted=len(counted), excluded=len(rows) - len(counted), labels=LABELS)
