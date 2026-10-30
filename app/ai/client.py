"""Single entry point for every AI call. Two things live here from Day 2 on:

1. The compliance guard: Imovirtual's robots.txt carries `Content-Signal: ai-input=no`, so no text that came
   from Imovirtual may ever be sent to a model. Every AI helper calls `assert_llm_allowed` first.
2. The spend ledger (`ai_spend`) and the monthly cap check used by the budget guard.

The Anthropic client itself (criteria parsing, site profiling) arrives with Days 5 and 6.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import AiSpend

FORBIDDEN_SOURCES = {"imovirtual"}


class AiGuardError(RuntimeError):
    """Raised when content from a source that forbids AI input is about to reach a model."""


class BudgetExceeded(RuntimeError):
    """Raised when the monthly AI cap is reached; callers fall back to the non-AI path."""


def assert_llm_allowed(source_key: str | None, *, purpose: str, signal_ai_input: str | None = None) -> None:
    """`signal_ai_input` is the source's own `Content-Signal: ai-input=…`, as cached in `sources`.

    The hard-coded `FORBIDDEN_SOURCES` stays as belt-and-braces for the one source we know declares `no`,
    but it can never be the whole guard: agency sources are created at runtime, so the decision has to come
    from their stored signal (Plan_Day5.md section 5 item 2)."""
    if signal_ai_input and signal_ai_input.strip().lower() == "no":
        raise AiGuardError(f"{source_key or 'source'} declares Content-Signal ai-input=no; purpose={purpose}")
    if source_key and source_key.lower() in FORBIDDEN_SOURCES:
        raise AiGuardError(f"{source_key} content may not be sent to an LLM (Content-Signal ai-input=no); purpose={purpose}")


def assert_source_allows_llm(source, *, purpose: str) -> None:
    """The data-driven guard: works for any `sources` row, including the agency sites discovered at runtime."""
    assert_llm_allowed(source.key, purpose=purpose, signal_ai_input=source.signal_ai_input)


def month_spend_cents(session: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(session.scalar(select(func.coalesce(func.sum(AiSpend.cost_cents), 0)).where(AiSpend.created_at >= start)) or 0)


def budget_guard(session: Session, *, estimate_cents: int = 0) -> None:
    cap = settings.ai_cap_cents_month
    if cap and month_spend_cents(session) + estimate_cents > cap:
        raise BudgetExceeded(f"monthly AI cap of {cap} cents reached")


def record_spend(session: Session, *, provider: str, operation: str, model: str | None = None,
                 tokens_in: int = 0, tokens_out: int = 0, audio_seconds: float = 0.0, cost_cents: int = 0) -> AiSpend:
    row = AiSpend(provider=provider, operation=operation, model=model, tokens_in=tokens_in, tokens_out=tokens_out,
                  audio_seconds=audio_seconds, cost_cents=cost_cents)
    session.add(row)
    session.flush()
    return row
