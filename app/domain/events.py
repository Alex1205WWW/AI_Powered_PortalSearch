"""The "novo para o comprador" cases, derived from what changed when a listing was (re)matched.

  new             the buyer sees this property for the first time (new listing, or a known listing that
                  started matching for a reason other than a price drop)
  price_drop      a known property got cheaper (into the range, or while already in range)
  data_completed  a "possível" match became "confirmada" because the listing now states a must-have
  criteria_changed  (Day 6) the buyer's criteria were edited and this property newly qualifies

Initial runs never produce events: the first bulk list goes to the panel, not to Telegram.
"""
from __future__ import annotations

from app.domain.matching import GRADE_CONFIRMED, GRADE_POSSIBLE, MatchResult

GRADE_OUT = "fora"  # stored on a match row whose listing no longer satisfies the criteria


def derive(*, result: MatchResult, created: bool, previous_grade: str | None, listing_is_new: bool,
           price_changed: bool, old_price: int | None, new_price: int | None, initial: bool) -> list[str]:
    if initial or not result.ok:
        return []
    dropped = price_changed and old_price is not None and new_price is not None and new_price < old_price
    if created:
        if listing_is_new:
            return ["new"]
        return ["price_drop"] if dropped else ["new"]
    reasons: list[str] = []
    if previous_grade == GRADE_OUT:
        reasons.append("price_drop" if dropped else "new")
    else:
        if dropped:
            reasons.append("price_drop")
        if previous_grade == GRADE_POSSIBLE and result.grade == GRADE_CONFIRMED:
            reasons.append("data_completed")
    return reasons
