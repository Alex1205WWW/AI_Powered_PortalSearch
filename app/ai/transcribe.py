"""Her voice notes -> text, via OpenAI Whisper, with the cost recorded per call.

Telegram sends voice notes as OGG/Opus, which Whisper accepts directly, so nothing is transcoded and
`ffmpeg` is not a dependency. `language="pt"` is passed explicitly: left to auto-detect, a short note with
place names ("Oeiras", "Alcântara") is sometimes read as Spanish and the zones come back mangled.

Billing is per minute of audio (~0.006 USD), so a 30-second note costs well under a cent. The duration
Telegram reports on the voice message is used for the ledger - it is authoritative and free, whereas asking
the API for it is not.
"""
from __future__ import annotations

import io
import logging

from sqlalchemy.orm import Session

from app.ai import client as ai
from app.ai import pricing

log = logging.getLogger(__name__)

MODEL = "whisper-1"
MAX_BYTES = 25 * 1024 * 1024  # OpenAI's own limit; Telegram caps voice notes well below it
LANGUAGE = "pt"


class AudioTooLong(ValueError):
    """Longer than the API accepts. She is told to send a shorter note rather than losing the message."""


def transcribe(session: Session, audio: bytes, *, filename: str = "nota.ogg",
               duration_s: float | None = None) -> str:
    """Raises `ai.AiUnavailable` when no key is set, `ai.BudgetExceeded` at the cap, `AudioTooLong` if big.

    The spend is recorded even though the text is what the caller wants: an untracked call is how a monthly
    cap gets quietly overrun.
    """
    if len(audio) > MAX_BYTES:
        raise AudioTooLong(f"{len(audio)} bytes excede o limite de {MAX_BYTES}")
    ai.budget_guard(session)
    stream = io.BytesIO(audio)
    stream.name = filename  # the SDK infers the format from the filename
    response = ai.openai_client().audio.transcriptions.create(model=MODEL, file=stream, language=LANGUAGE)
    seconds = float(duration_s or 0.0)
    ai.record_spend(session, provider="openai", operation="transcribe", model=MODEL,
                    audio_seconds=seconds, cost_cents=pricing.stt_cost_cents(seconds))
    text = (getattr(response, "text", "") or "").strip()
    log.info("voice note transcribed: %.0fs -> %d chars", seconds, len(text))
    return text
