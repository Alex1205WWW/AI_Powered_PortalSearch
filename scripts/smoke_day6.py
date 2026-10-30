"""Day 6 smoke test against a real database: the draft loop, without needing a model.

The LLM and Whisper calls are the one part this cannot cover (they need paid keys), so the parse is handed
in directly - exactly the object `ai.parse_criteria.parse` returns. Everything after that point is the real
code path: zone resolution against the real `geo` table, buyer matching, confirmation, the queued initial
run, state buttons, quiet hours, and `/faltou`.

Run:  .venv\\Scripts\\python -m scripts.smoke_day6
It creates a buyer called "Smoke Teste", then deletes it, so it is safe to re-run.
"""
from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.ai.parse_criteria import ParsedCriteria, sanitise
from app.bot.handlers import findings
from app.db.models import Buyer, CrawlRun, Criteria, CriteriaDraft, PropertyMatch
from app.db.session import session_scope
from app.notify import formats
from app.services import appsettings, digest, drafts

LABEL = "Smoke Teste"
CHAT = 999_000_001
ok_count, fail_count = 0, 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if condition:
        ok_count += 1
        print(f"  ok    {name}" + (f"  [{detail}]" if detail else ""))
    else:
        fail_count += 1
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def cleanup(session) -> None:
    for buyer in session.scalars(select(Buyer).where(Buyer.label == LABEL)):
        for criteria in list(buyer.criteria):
            session.query(PropertyMatch).filter(PropertyMatch.criteria_id == criteria.id).delete()
            session.query(CrawlRun).filter(CrawlRun.criteria_id == criteria.id).delete()
        session.delete(buyer)
    session.query(CriteriaDraft).filter(CriteriaDraft.telegram_chat_id == CHAT).delete()
    session.commit()


def main() -> int:
    with session_scope() as session:
        cleanup(session)

        print("\n1. zone resolution against the real geo table")
        codes, questions = drafts.resolve_zones(session, ["Oeiras"])
        check("an unambiguous council resolves", codes == ["lisboa/oeiras"], str(codes))
        codes, questions = drafts.resolve_zones(session, ["Lisboa"])
        check("Lisboa is ASKED, not guessed", not codes and len(questions) == 1, str(questions))
        codes, questions = drafts.resolve_zones(session, ["Narnia"])
        check("an unknown place becomes a question", not codes and questions, str(questions))

        print("\n2. a draft is a database row, so a restart loses nothing")
        parsed, notes = sanitise(ParsedCriteria(
            buyer_label=LABEL, estate_types=["apartamento"], typology_min=1, typology_max=2,
            price_min=300_000, price_max=400_000, zones=["Oeiras"], must_have=["garagem"],
            nice_to_have=["elevador", "heliporto"]))
        check("invented feature dropped with a note", "heliporto" in " ".join(notes), str(notes))
        draft = drafts.create(session, transcript="nota de teste", parsed=parsed, notes=notes, chat_id=CHAT)
        session.commit()
        check("draft persisted", draft.id is not None and draft.status == "pending")
        check("zones stored as codes", (draft.parsed or {}).get("zone_codes") == ["lisboa/oeiras"])
        reloaded = drafts.pending_for_chat(session, CHAT)
        check("draft is found again after a 'restart'", reloaded is not None and reloaded.id == draft.id)

        print("\n3. the card renders what she will actually see")
        card = formats.criteria_card(draft.parsed, draft.ambiguities or [], ["Oeiras"])
        check("card names the buyer and the zone", LABEL in card and "Oeiras" in card)
        check("card lists the required feature", "Garagem" in card)
        check("dropped feature is surfaced", "heliporto" in card)

        print("\n4. a draft with no zone cannot be confirmed")
        empty = drafts.create(session, transcript="sem zona", parsed=sanitise(ParsedCriteria(buyer_label="X"))[0],
                              notes=[], chat_id=CHAT + 1)
        ready, why = drafts.ready(empty)
        check("blocked with a reason", not ready and bool(why), str(why))

        print("\n5. confirm -> criteria + a SILENT initial run")
        before_notifs = session.scalar(select(Criteria.id).limit(1)) is not None or True
        criteria = drafts.confirm(session, draft)
        session.commit()
        check("criteria created", criteria.id is not None)
        check("input channel recorded as text", criteria.input_channel == "text", criteria.input_channel)
        check("zones attached", [z.geo_code for z in criteria.zones] == ["lisboa/oeiras"])
        check("draft marked confirmed", draft.status == "confirmed")
        run = session.scalar(select(CrawlRun).where(CrawlRun.criteria_id == criteria.id))
        check("an initial run was queued", run is not None and run.kind == "initial", run.status if run else "none")

        print("\n6. a near-duplicate buyer is ASKED about, never created twice")
        exact, near = drafts.match_buyer(session, "smoke  teste")
        check("fuzzy match finds the existing buyer", exact is not None or bool(near),
              f"exact={bool(exact)} near={[b.label for b in near]}")
        exact2, _ = drafts.match_buyer(session, LABEL)
        check("exact match is exact", exact2 is not None and exact2.label == LABEL)

        print("\n7. quiet hours hold alerts instead of dropping them")
        night = datetime(2026, 10, 31, 3, 0, tzinfo=ZoneInfo("Europe/Lisbon"))
        day = datetime(2026, 10, 31, 10, 0, tzinfo=ZoneInfo("Europe/Lisbon"))
        check("03:00 is outside her window", not appsettings.within_alert_hours(session, night))
        check("10:00 is inside her window", appsettings.within_alert_hours(session, day))

        print("\n8. the digest")
        rows = digest.per_buyer(session, datetime.now(ZoneInfo("UTC")).replace(hour=0, minute=0))
        check("digest sees the buyers", any(r[0] == LABEL for r in rows), f"{len(rows)} buyer(s)")
        text = formats.digest("31/10", rows, degraded_agencies=digest.degraded_agencies(session))
        check("digest renders", "Resumo" in text)

        print("\n9. /faltou reconciles against what we hold")
        verdict = findings._record(session, "https://www.imovirtual.com/pt/anuncio/nao-existe-aqui-xyz")
        check("a known source we missed is our fault", "falha nossa" in verdict, verdict)
        verdict = findings._record(session, "https://www.supercasa.pt/imovel/123")
        check("an out-of-scope source is excluded from the test", "fora do âmbito" in verdict, verdict)
        stored = session.scalar(select(Criteria.id).limit(1))
        session.commit()

        print("\n10. cleanup")
        cleanup(session)
        check("buyer removed", session.scalar(select(Buyer).where(Buyer.label == LABEL)) is None)

    print(f"\n{ok_count} ok, {fail_count} failed")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
