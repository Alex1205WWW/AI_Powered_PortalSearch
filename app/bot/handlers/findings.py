"""`/faltou <link>` — a property she found by hand that the system did not bring her.

This is the acceptance-test instrument, not a convenience: the 7-day test is "at least 80 % of what she
finds manually", and `manual_findings` is where the denominator comes from. Capturing it the moment she
notices costs her one message; reconstructing it afterwards from memory is impossible.

Each finding is immediately reconciled against what we already hold, so the verdict is recorded with the
reason rather than decided later:
  `ja_tinha`       we had this listing - it was in the panel, so the miss was in the alerting, not the crawl
  `fonte_acordada` the URL is from a source we read, and we did not have it - a genuine gap
  `fora_do_ambito` the URL is from a source explicitly out of scope (Idealista direct, SUPERCASA, OLX...)
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.common import allowed, in_db
from app.db.models import Listing, ManualFinding, Source

log = logging.getLogger(__name__)

VERDICTS = {
    "ja_tinha": "Já tínhamos este imóvel — estava no painel.",
    "fonte_acordada": "Registado como falha nossa: é de uma fonte que lemos e não o trouxemos.",
    "fora_do_ambito": "Registado. É de uma fonte fora do âmbito acordado, por isso não conta para o teste.",
}


async def faltou(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    url = (" ".join(context.args or []).strip() or "").split()[0] if context.args else ""
    if not url.startswith("http"):
        await update.message.reply_text(
            "Envie o link: <code>/faltou https://…</code>", parse_mode="HTML")
        return
    outcome = await in_db(lambda s: _record(s, url))
    await update.message.reply_text(outcome, parse_mode="HTML")


def _record(session, url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    existing = session.scalar(select(Listing).where(Listing.url == url))
    if existing is None:
        # Same property, different URL spelling (a tracking query string, http vs https).
        stem = url.split("?")[0].rstrip("/")
        existing = session.scalar(select(Listing).where(Listing.url.like(f"{stem}%")))
    in_agreed = _is_agreed_source(session, host)
    if existing is not None:
        verdict = "ja_tinha"
    else:
        verdict = "fonte_acordada" if in_agreed else "fora_do_ambito"
    session.add(ManualFinding(url=url[:2000], matched_listing_id=existing.id if existing else None,
                              in_agreed_source=in_agreed, verdict=verdict))
    session.flush()
    log.info("manual finding recorded: %s -> %s", host, verdict)
    return VERDICTS[verdict]


def _is_agreed_source(session, host: str) -> bool:
    """Is this host one of the sources we actually read? Compared against `sources`, not a hard-coded list,
    so adding a mediadora automatically brings her findings from it into scope."""
    for source in session.scalars(select(Source).where(Source.enabled.is_(True))):
        base = urlparse(source.base_url or "").netloc.lower().removeprefix("www.")
        if base and (host == base or host.endswith("." + base) or base.endswith("." + host)):
            return True
    return False
