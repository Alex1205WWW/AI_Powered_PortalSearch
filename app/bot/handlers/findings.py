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
from app.db.models import Buyer, Listing, ManualFinding, Source
from app.services import appsettings

log = logging.getLogger(__name__)

NO_BUYER = """

Não percebi para que comprador é. Diga o nome a seguir ao link (<code>/faltou https://… Rosa</code>) para contar na pontuação dele."""

VERDICTS = {
    "ja_tinha": "Já tínhamos este imóvel — estava no painel.",
    "fonte_acordada": "Registado como falha nossa: é de uma fonte que lemos e não o trouxemos.",
    "fora_do_ambito": "Registado. É de uma fonte fora do âmbito acordado, por isso não conta para o teste.",
}


async def faltou(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    args = list(context.args or [])
    url = args[0] if args else ""
    # Anything after the link names the buyer. The test is scored per buyer, so a finding with no buyer
    # can only ever land in a total - and she is reporting while walking between viewings, so asking a
    # follow-up question would lose it.
    buyer_hint = " ".join(args[1:]).strip()
    if not url.startswith("http"):
        await update.message.reply_text(
            "Envie o link e o comprador: <code>/faltou https://… Rosa</code>", parse_mode="HTML")
        return
    outcome = await in_db(lambda s: _record(s, url, buyer_hint))
    await update.message.reply_text(outcome, parse_mode="HTML")


def _record(session, url: str, buyer_hint: str = "") -> str:
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
    # Only decidable when we hold the advert. A listing we never saw has no publication date here, and
    # `in_test_window` answers None rather than guessing - the scoreboard then shows it as pending.
    in_window = appsettings.in_test_window(session, existing.published_at if existing else None)
    buyer = _match_buyer(session, buyer_hint)
    session.add(ManualFinding(url=url[:2000], buyer_id=buyer.id if buyer else None,
                              matched_listing_id=existing.id if existing else None,
                              in_agreed_source=in_agreed, published_in_window=in_window,
                              verdict=verdict))
    session.flush()
    log.info("manual finding recorded: %s -> %s (buyer=%s, in_window=%s)",
             host, verdict, buyer.label if buyer else None, in_window)
    answer = VERDICTS[verdict]
    if buyer is None:
        answer += NO_BUYER
    return answer


def _match_buyer(session, hint: str) -> Buyer | None:
    """Her shorthand, not an exact label: "Rosa" must find "Rosa Brás". Two matches is not a match -
    attaching the finding to the wrong buyer corrupts the number it exists to measure."""
    text = (hint or "").strip().lower()
    if not text:
        return None
    hits = [b for b in session.scalars(select(Buyer)) if text in (b.label or "").lower()]
    return hits[0] if len(hits) == 1 else None


def _is_agreed_source(session, host: str) -> bool:
    """Is this host one of the sources we actually read? Compared against `sources`, not a hard-coded list,
    so adding a mediadora automatically brings her findings from it into scope."""
    for source in session.scalars(select(Source).where(Source.enabled.is_(True))):
        base = urlparse(source.base_url or "").netloc.lower().removeprefix("www.")
        if base and (host == base or host.endswith("." + base) or base.endswith("." + host)):
            return True
    return False
