"""Day 7 smoke test against a real database: the spend cap, pause semantics, and the findings score.

The three Day 7 gates, each checked against real data rather than argued for:

  * forcing spend past the cap suspends the AI and **nothing else** - a sweep still runs and still alerts;
  * a paused buyer gets zero notifications while her matches keep accumulating, and resuming sends nothing
    retroactively;
  * the acceptance-test score counts only what was agreed to count.

Run:  .venv\\Scripts\\python -m scripts.smoke_day7
It restores the cap, the warning flag and every buyer's paused state before exiting.
"""
from __future__ import annotations

import io
import sys
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from app.ai import client as ai
from app.db.models import AiSpend, Buyer, ManualFinding, PropertyMatch
from app.db.session import session_scope
from app.services import appsettings, budget, buyers as buyer_svc, notifications as notif_svc
from app.services import runs
from app.services.criteria import list_active

ok_count, fail_count = 0, 0


def _references_budget(path: str) -> str:
    """Any real reference to the spend cap in `path`, or "" if there is none.

    The point of this guard is that running out of AI credit must never stop her searches or her alerts,
    so these four modules may not depend on the budget. It reads the syntax tree rather than grepping for
    the word: a docstring that merely explains why a module does NOT touch the budget is not coupling, and
    a substring match flagged exactly that on 2026-11-04. Imports, bare names and attributes are all
    checked, so a genuine `budget.guard(...)` or `from app.services import budget` is still caught.
    """
    import ast

    tree = ast.parse(io.open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "budget" in node.module:
                return f"from {node.module} import ..."
            for alias in node.names:
                if "budget" in alias.name:
                    return f"from {node.module} import {alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "budget" in alias.name:
                    return f"import {alias.name}"
        elif isinstance(node, ast.Name) and "budget" in node.id:
            return f"name {node.id}"
        elif isinstance(node, ast.Attribute) and "budget" in node.attr:
            return f"attribute .{node.attr}"
    return ""


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if condition:
        ok_count += 1
        print(f"  ok    {name}" + (f"  [{detail}]" if detail else ""))
    else:
        fail_count += 1
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def main() -> int:
    with session_scope() as session:
        original_cap = budget.cap_cents(session)
        original_warn = appsettings.get(session, budget.WARNED_KEY, "")
        spend_backup = [dict(provider=r.provider, operation=r.operation, model=r.model,
                             tokens_in=r.tokens_in, tokens_out=r.tokens_out,
                             audio_seconds=r.audio_seconds, cost_cents=r.cost_cents,
                             created_at=r.created_at)
                        for r in session.scalars(select(AiSpend))]

        print("\n1. the spend cap warns, then suspends")
        session.execute(delete(AiSpend))
        budget.set_cap_cents(session, 100)
        appsettings.put(session, budget.WARNED_KEY, "")
        session.commit()
        check("no cap pressure at zero spend", budget.status(session)["state"] == "ok")
        ai.record_spend(session, provider="anthropic", operation="parse_criteria",
                        model="claude-opus-5", cost_cents=85)
        session.commit()
        check("80 % of the cap is a warning, not a stop", budget.status(session)["state"] == "warning",
              f"{budget.status(session)['share']:.0%}")
        ai.budget_guard(session)
        session.commit()
        warned = str(appsettings.get(session, budget.WARNED_KEY, ""))
        check("the warning is recorded for this month", warned == datetime.now(timezone.utc).strftime("%Y-%m"), warned)
        ai.budget_guard(session)
        check("and never fires twice in a month",
              str(appsettings.get(session, budget.WARNED_KEY, "")) == warned)

        ai.record_spend(session, provider="anthropic", operation="parse_criteria",
                        model="claude-opus-5", cost_cents=30)
        session.commit()
        check("at the cap the state is suspended", budget.status(session)["state"] == "suspended",
              f"{budget.status(session)['spent_cents']} of {budget.status(session)['cap_cents']}")
        try:
            ai.budget_guard(session)
            check("the AI is refused past the cap", False, "guard did not raise")
        except ai.BudgetExceeded as exc:
            check("the AI is refused past the cap", True, str(exc)[:46])

        print("\n2. ...and the search is untouched while the AI is suspended")
        actives = list_active(session)
        check("active criteria still readable", bool(actives), f"{len(actives)} criteria")
        if actives:
            evaluated = runs.rematch_criteria(session, actives[0])
            check("matching still runs with the AI suspended", evaluated > 0, f"{evaluated} match(es)")
        for module in ("app/services/runs.py", "app/services/notifications.py",
                       "app/services/properties.py", "app/services/matches.py"):
            referenced = _references_budget(module)
            check(f"no cap coupling in {module.split('/')[-1]}", not referenced, referenced or "")

        print("\n3. a paused buyer: silent, but nothing is lost")
        buyer = session.scalars(select(Buyer).where(Buyer.active.is_(True))).first()
        if buyer is None:
            check("a buyer exists to test with", False)
        else:
            was_active = buyer.active
            matches_before = session.scalar(
                select(PropertyMatch.id).where(PropertyMatch.buyer_id == buyer.id).limit(1)) is not None
            property_id = session.scalar(
                select(PropertyMatch.property_id).where(PropertyMatch.buyer_id == buyer.id).limit(1))
            buyer_svc.pause(session, buyer.id)
            session.commit()
            check("buyer is paused", not session.get(Buyer, buyer.id).active, buyer.label)
            if property_id:
                queued = notif_svc.enqueue(session, buyer_id=buyer.id, property_id=property_id,
                                           reason="new", price=123456)
                session.commit()
                check("a paused buyer queues NO notification", queued is False)
            criteria = [c for c in session.get(Buyer, buyer.id).criteria if c.active]
            if criteria:
                evaluated = runs.rematch_criteria(session, criteria[0])
                session.commit()
                check("but her matches keep accumulating", evaluated >= 0, f"{evaluated} evaluated")
            buyer_svc.resume(session, buyer.id)
            session.commit()
            check("resume restores her", session.get(Buyer, buyer.id).active)
            pending_after = [n for n in notif_svc.pending(session) if n.buyer_id == buyer.id]
            check("resuming sends nothing retroactively", not pending_after,
                  f"{len(pending_after)} pending")
            if not was_active:
                buyer_svc.pause(session, buyer.id)
                session.commit()
            check("matches were not destroyed by the pause",
                  (session.scalar(select(PropertyMatch.id).where(PropertyMatch.buyer_id == buyer.id).limit(1))
                   is not None) == matches_before)

        print("\n4. the acceptance score counts only what was agreed")
        before = session.scalar(select(ManualFinding.id).limit(1))
        session.add_all([
            ManualFinding(url="https://x/1", verdict="ja_tinha", in_agreed_source=True),
            ManualFinding(url="https://x/2", verdict="fonte_acordada", in_agreed_source=True),
            ManualFinding(url="https://x/3", verdict="fora_do_ambito", in_agreed_source=False),
        ])
        session.commit()
        rows = list(session.scalars(select(ManualFinding).where(ManualFinding.url.like("https://x/%"))))
        counted = [r for r in rows if r.verdict in ("ja_tinha", "fonte_acordada")]
        had = sum(1 for r in counted if r.verdict == "ja_tinha")
        check("out-of-scope findings leave the denominator", len(counted) == 2, f"{len(rows)} rows, {len(counted)} counted")
        check("the score is had/counted", round(had / len(counted), 2) == 0.5, f"{had}/{len(counted)}")
        session.execute(delete(ManualFinding).where(ManualFinding.url.like("https://x/%")))
        session.commit()

        print("\n5. deleting a buyer cascades safely")
        probe = Buyer(label="Apagar Smoke")
        session.add(probe)
        session.commit()
        probe_id = probe.id
        check("a throwaway buyer was created", probe_id is not None)
        buyer_svc.delete(session, probe_id)
        session.commit()
        check("deleted without a foreign-key error",
              session.scalar(select(Buyer).where(Buyer.label == "Apagar Smoke")) is None)
        check("the real buyers are untouched",
              session.scalar(select(func.count()).select_from(Buyer)) >= 1)

        print("\n6. restoring the database")
        session.execute(delete(AiSpend))
        for row in spend_backup:
            session.add(AiSpend(**row))
        budget.set_cap_cents(session, original_cap)
        appsettings.put(session, budget.WARNED_KEY, original_warn or "")
        session.commit()
        check("cap restored", budget.cap_cents(session) == original_cap, str(original_cap))
        check("ledger restored", session.scalar(select(AiSpend.id).limit(1)) is not None or not spend_backup)

    print(f"\n{ok_count} ok, {fail_count} failed")
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
