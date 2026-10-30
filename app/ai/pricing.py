"""What a model call costs, in one place, so `ai_spend` is real money and not a guess.

Rates are USD per million tokens, from the Anthropic pricing table (checked 2026-10-31). Only the models this
project can be configured with are listed; an unknown model falls back to the most expensive known rate so a
misconfiguration can never silently under-report spend against her monthly cap.

Cache accounting: a cache write costs ~1.25x the input rate, a cache read ~0.1x. We do not cache today - the
criteria prompt is a few hundred tokens - but the ledger handles it so enabling caching later does not make
the numbers wrong.
"""
from __future__ import annotations

from dataclasses import dataclass

MTOK = 1_000_000


@dataclass(frozen=True)
class Rate:
    input_usd: float
    output_usd: float

    def cost_usd(self, *, tokens_in: int, tokens_out: int, cache_write: int = 0, cache_read: int = 0) -> float:
        return (
            tokens_in * self.input_usd
            + tokens_out * self.output_usd
            + cache_write * self.input_usd * 1.25
            + cache_read * self.input_usd * 0.10
        ) / MTOK


# Anthropic, USD / MTok
LLM_RATES: dict[str, Rate] = {
    "claude-opus-5": Rate(5.00, 25.00),
    "claude-sonnet-5": Rate(2.00, 10.00),
    "claude-haiku-4-5": Rate(1.00, 5.00),
}
MOST_EXPENSIVE = Rate(10.00, 50.00)  # fallback for an unrecognised model id

# OpenAI Whisper is billed per minute of audio, not per token.
WHISPER_USD_PER_MINUTE = 0.006


def llm_rate(model: str | None) -> Rate:
    return LLM_RATES.get((model or "").strip(), MOST_EXPENSIVE)


def llm_cost_cents(model: str | None, *, tokens_in: int, tokens_out: int,
                   cache_write: int = 0, cache_read: int = 0) -> int:
    """Rounded UP to the cent: under-reporting spend against a cap is the dangerous direction."""
    usd = llm_rate(model).cost_usd(tokens_in=tokens_in, tokens_out=tokens_out,
                                  cache_write=cache_write, cache_read=cache_read)
    return _ceil_cents(usd)


def stt_cost_cents(audio_seconds: float) -> int:
    return _ceil_cents((audio_seconds / 60.0) * WHISPER_USD_PER_MINUTE)


def _ceil_cents(usd: float) -> int:
    cents = usd * 100
    whole = int(cents)
    return whole + (1 if cents > whole else 0)
