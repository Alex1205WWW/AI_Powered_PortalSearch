"""Single entry point for every AI call. Two things live here from Day 2 on:

1. The compliance guard: Imovirtual's robots.txt carries `Content-Signal: ai-input=no`, so no text that came
   from Imovirtual may ever be sent to a model. Every AI helper calls `assert_llm_allowed` first.
2. The spend ledger (`ai_spend`) and the monthly cap check used by the budget guard.

The Anthropic client itself (criteria parsing, site profiling) arrives with Days 5 and 6.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import pricing
from app.config import settings
from app.db.models import AiSpend

log = logging.getLogger(__name__)

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
    """The one place the monthly cap is enforced - every model call passes through here.

    Delegates to `services.budget`, which owns the cap value (stored in `app_settings` so she can change it
    without a redeploy) and fires the 80 % warning. Imported locally because `services.budget` reads the
    ledger this module writes."""
    from app.services import budget as budget_svc

    try:
        budget_svc.ensure_within(session, estimate_cents=estimate_cents)
    except budget_svc.BudgetExceeded as exc:
        raise BudgetExceeded(str(exc)) from exc


def record_spend(session: Session, *, provider: str, operation: str, model: str | None = None,
                 tokens_in: int = 0, tokens_out: int = 0, audio_seconds: float = 0.0, cost_cents: int = 0) -> AiSpend:
    row = AiSpend(provider=provider, operation=operation, model=model, tokens_in=tokens_in, tokens_out=tokens_out,
                  audio_seconds=audio_seconds, cost_cents=cost_cents)
    session.add(row)
    session.flush()
    return row


# --- the real clients and the spend ledger (Day 6) ---------------------------------------------
# Every model call in this project goes through `call_llm` or `ai.transcribe`, so the budget guard and the
# `ai_spend` ledger cannot be bypassed by a new caller forgetting to record anything.

def anthropic_client():
    """Lazy, so importing this module never requires a key (the test suite imports it constantly)."""
    import anthropic

    if not settings.anthropic_api_key:
        raise AiUnavailable("ANTHROPIC_API_KEY não está configurada.")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def openai_client():
    import openai

    if not settings.openai_api_key:
        raise AiUnavailable("OPENAI_API_KEY não está configurada.")
    return openai.OpenAI(api_key=settings.openai_api_key)


class AiUnavailable(RuntimeError):
    """The model cannot be reached right now: no key, a dead key, no credit, or the provider is down.

    Never fatal, and that is the point - every caller falls back to the structured form in the panel, which
    needs no model at all. It exists so that a provider failure reaches her as one sentence in Portuguese
    telling her what to do instead, rather than as silence.
    """


# Provider SDK errors are translated into `AiUnavailable` here, at the boundary, because that is the only
# exception the bot handlers are written to catch. Letting a raw `openai.RateLimitError` or
# `anthropic.APIStatusError` escape means the handler dies, `_on_error` logs it, and she gets NO REPLY to
# her voice note - which is exactly what would have happened on her first attempt: her OpenAI key
# authenticates but has no credit (429 `insufficient_quota`, measured 2026-11-04).
def _provider_reason(exc: Exception) -> str:
    """A short Portuguese reason she can act on, from a provider exception."""
    text = str(exc)
    if "insufficient_quota" in text or "no credits remaining" in text or "credit_balance" in text:
        return "a conta da IA está sem saldo"
    status = getattr(exc, "status_code", None)
    if status == 401 or "authentication" in text.lower() or "invalid_api_key" in text:
        return "a chave da IA foi recusada"
    if status == 429:
        return "a IA está a limitar os pedidos; tente dentro de um minuto"
    if status and 500 <= int(status) < 600:
        return "o serviço de IA está com problemas"
    return "o serviço de IA não respondeu"


def as_unavailable(exc: Exception) -> AiUnavailable:
    return AiUnavailable(_provider_reason(exc))


def usage_of(response) -> dict:
    """`response.usage` -> the four counters the ledger stores. Tolerant of fields a model may not send."""
    usage = getattr(response, "usage", None)
    return {
        "tokens_in": int(getattr(usage, "input_tokens", 0) or 0),
        "tokens_out": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_write": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    }


def record_llm_spend(session: Session, *, operation: str, model: str, response) -> AiSpend:
    counts = usage_of(response)
    return record_spend(session, provider="anthropic", operation=operation, model=model,
                        tokens_in=counts["tokens_in"], tokens_out=counts["tokens_out"],
                        cost_cents=pricing.llm_cost_cents(model, **counts))


SCHEMA_TOOL_NAME = "registar_criterios"


def parse_structured(session: Session, *, operation: str, system: str, user: str, output_format,
                     source_key: str | None = None, model: str | None = None, effort: str = "low",
                     max_tokens: int = 2000):
    """One structured LLM call: schema in, validated object out, spend recorded, budget respected.

    The schema is delivered as a **tool**, not as `messages.parse(output_format=...)`.

    That is not a style preference. With the real key in place (2026-11-04, the first time this path could
    ever run) `messages.parse` answered **HTTP 400 "Schema is too complex"** for `ParsedCriteria`, so voice
    intake and note parsing were dead in production - deliverable 3 - and no test could have caught it
    because there was no key to call with. Measured while narrowing it down:

        output_format, 13 fields / 6 arrays  -> 400 too complex
        output_format, 13 fields / 3 arrays  -> 400 too complex
        output_format, 10 fields / 2 arrays  -> OK
        tool_choice,   13 fields / 6 arrays  -> OK

    Neither the nullable unions nor the field descriptions were the cause; 6 nullable fields alone passed.
    The limit is a function of field count and array count together, and the criteria schema is simply past
    it. Tool input schemas are validated far more permissively, so the whole contract survives - `exclude`,
    `nice_to_have` and `ambiguities` included - and the alternative would have been to delete fields she was
    promised.

    What is lost by not using `output_format` is the server-side guarantee, so the response is validated
    here with Pydantic instead. A malformed answer still raises rather than becoming half-filled criteria.

    `effort="low"` is deliberate and still applies with tools (verified). Opus 5 thinks adaptively by
    default and this is a short extraction from a few sentences of Portuguese; low effort keeps one parse
    near 1.7 cents, which is what keeps the quoted 9-15 USD/month honest. Do not pass
    `thinking.budget_tokens` - Opus 5 rejects it with a 400.
    """
    assert_llm_allowed(source_key, purpose=operation)
    budget_guard(session)
    chosen = model or settings.llm_model
    tool = {
        "name": SCHEMA_TOOL_NAME,
        "description": "Registe os critérios de pesquisa extraídos da nota.",
        "input_schema": output_format.model_json_schema(),
    }
    client = anthropic_client()
    try:
        response = client.messages.create(
            model=chosen,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort},
            tools=[tool],
            tool_choice={"type": "tool", "name": SCHEMA_TOOL_NAME},
        )
    except (AiUnavailable, BudgetExceeded, AiGuardError):
        raise
    except Exception as exc:  # noqa: BLE001 - any provider failure must degrade, never surface raw
        log.warning("llm call failed (%s): %s", operation, exc)
        raise as_unavailable(exc) from exc
    # Recorded before any raise below, so a refused or malformed answer is still billed in the ledger -
    # it cost real money and her cap must see it.
    record_llm_spend(session, operation=operation, model=chosen, response=response)
    if getattr(response, "stop_reason", None) == "refusal":
        raise AiRefused(f"o modelo recusou o pedido ({operation})")
    block = next((b for b in getattr(response, "content", []) if getattr(b, "type", None) == "tool_use"), None)
    if block is None:
        raise AiRefused(f"resposta sem conteúdo estruturado ({operation})")
    try:
        return output_format.model_validate(block.input)
    except Exception as exc:  # pydantic.ValidationError, kept broad so callers always get AiRefused
        raise AiRefused(f"resposta estruturada inválida ({operation}): {exc}") from exc


class AiRefused(RuntimeError):
    """The model declined or returned nothing usable. Callers fall back to the structured form."""
