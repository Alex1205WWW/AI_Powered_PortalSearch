"""The confirmation loop: her note becomes a draft, she checks it, and only then does a search exist.

Nothing she dictates creates a search directly. The draft is a database row, so a bot restart mid-conversation
loses nothing (a Day 6 gate), and the card can be re-rendered from it afterwards.

Zone resolution lives here rather than in the model: `geo.resolve.find` is asked for each place name she
said, and a name that is both a district and a council ("Lisboa") becomes a **question on the card**, never
a guess. That is the difference between a visibly wrong card she can fix and a silent mis-parse.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.parse_criteria import ParsedCriteria
from app.db.models import Buyer, Criteria, CriteriaDraft
from app.domain.criteria import CriteriaInput
from app.geo import resolve as geo_resolve

log = logging.getLogger(__name__)

STATUS_PENDING, STATUS_CONFIRMED, STATUS_CANCELLED = "pending", "confirmed", "cancelled"
LEVEL_WORD = {"district": "distrito", "council": "concelho", "parish": "freguesia"}


def create(session: Session, *, transcript: str, parsed: ParsedCriteria, notes: list[str],
           chat_id: int | None = None, file_id: str | None = None) -> CriteriaDraft:
    """Store the parse plus everything that still needs asking, resolved against the real geo table."""
    zones, questions = resolve_zones(session, parsed.zones)
    draft = CriteriaDraft(
        telegram_chat_id=chat_id,
        telegram_file_id=file_id,
        transcript=transcript.strip() or None,
        parsed={**parsed.model_dump(), "zone_codes": zones},
        ambiguities=list(parsed.ambiguities) + questions + notes,
        status=STATUS_PENDING,
    )
    session.add(draft)
    session.flush()
    return draft


def resolve_zones(session: Session, names: list[str]) -> tuple[list[str], list[str]]:
    """-> (geo codes, questions). A name we cannot place, or can place two ways, becomes a question.

    An ambiguous name is **not** dropped silently and **not** resolved to the first hit: she is asked. The
    draft stays unconfirmable until the zones are settled, because a search in the wrong Lisboa is worse
    than no search."""
    codes: list[str] = []
    questions: list[str] = []
    for name in dict.fromkeys(n.strip() for n in names if n and n.strip()):
        level, bare = geo_resolve.split_admin_level(name)
        # The qualifier narrows first; if it narrows to nothing (she said "concelho de Alcântara" and
        # Alcântara is a parish) fall back to the plain search so a wrong word costs her nothing.
        hits = geo_resolve.find(session, bare, level=level) if level else []
        if not hits:
            hits = geo_resolve.find(session, bare)
        if not hits:
            questions.append(f'Não reconheci a zona "{name}". Qual é o concelho?')
            continue
        if len(hits) == 1:
            codes.append(hits[0].code)
            continue
        options = " ou ".join(f"{LEVEL_WORD.get(h.level, h.level)} ({h.code})" for h in hits)
        questions.append(f'"{name}" pode ser {options}. Qual deles?')
    return codes, questions


def pending_for_chat(session: Session, chat_id: int) -> CriteriaDraft | None:
    return session.scalar(
        select(CriteriaDraft).where(CriteriaDraft.telegram_chat_id == chat_id,
                                    CriteriaDraft.status == STATUS_PENDING)
        .order_by(CriteriaDraft.id.desc()))


def ready(draft: CriteriaDraft) -> tuple[bool, str | None]:
    """Can this draft become a search? -> (ok, why not)."""
    parsed = draft.parsed or {}
    if not parsed.get("zone_codes"):
        return False, "Falta a zona: diga-me o concelho e volto a montar a pesquisa."
    if not parsed.get("buyer_label"):
        return False, "Falta o nome do comprador."
    return True, None


def match_buyer(session: Session, label: str) -> tuple[Buyer | None, list[Buyer]]:
    """-> (exact match, near matches). Near matches are offered before a second buyer is created: "Ana
    Santos" and "ana santos" are the same person, and a duplicate buyer silently splits her results."""
    wanted = (label or "").strip()
    if not wanted:
        return None, []
    rows = list(session.scalars(select(Buyer).order_by(Buyer.label)))
    exact = next((b for b in rows if b.label.strip().lower() == wanted.lower()), None)
    if exact is not None:
        return exact, []
    flat = _flat(wanted)
    near = [b for b in rows if _flat(b.label) == flat or flat in _flat(b.label) or _flat(b.label) in flat]
    return None, near


def _flat(text: str) -> str:
    return geo_resolve.slugify(text).replace("-", "")


def to_criteria_input(draft: CriteriaDraft, buyer_id: int) -> CriteriaInput:
    parsed = draft.parsed or {}
    return CriteriaInput(
        buyer_id=buyer_id,
        transaction=parsed.get("transaction") or "sale",
        estate_types=parsed.get("estate_types") or ["apartamento"],
        typology_min=parsed.get("typology_min"),
        typology_max=parsed.get("typology_max"),
        price_min=parsed.get("price_min"),
        price_max=parsed.get("price_max"),
        area_min=parsed.get("area_min"),
        must_have=parsed.get("must_have") or [],
        nice_to_have=parsed.get("nice_to_have") or [],
        exclude=parsed.get("exclude") or [],
        zones=parsed.get("zone_codes") or [],
        unknown_policy="show",
        source_text=draft.transcript,
        input_channel="voice" if draft.telegram_file_id else "text",
    )


def confirm(session: Session, draft: CriteriaDraft, *, buyer_id: int | None = None) -> Criteria:
    """Turn a checked draft into a real search, queueing its silent initial run.

    The initial run is what makes the first list land in the panel instead of as 300 Telegram messages -
    `runs.execute_initial` passes `initial=True`, so no event is derived from it."""
    from app.services import criteria as criteria_svc

    ok, why = ready(draft)
    if not ok:
        raise ValueError(why)
    if buyer_id is None:
        label = (draft.parsed or {}).get("buyer_label") or ""
        existing, _near = match_buyer(session, label)
        if existing is None:
            existing = Buyer(label=label.strip()[:80])
            session.add(existing)
            session.flush()
        buyer_id = existing.id
    criteria = criteria_svc.create(session, to_criteria_input(draft, buyer_id), queue_initial_run=True)
    draft.status = STATUS_CONFIRMED
    session.flush()
    log.info("draft %s confirmed -> criteria %s (buyer %s)", draft.id, criteria.id, buyer_id)
    return criteria


def cancel(session: Session, draft: CriteriaDraft) -> None:
    draft.status = STATUS_CANCELLED
    session.flush()


def apply_correction(session: Session, draft: CriteriaDraft, correction: str) -> CriteriaDraft:
    """She says what was wrong; the note and the correction are re-parsed together.

    Re-parsing both is what makes "não, é Oeiras e não Lisboa" work: the model sees the original note, so
    the correction does not have to repeat everything she already said."""
    from app.ai import parse_criteria

    combined = f"{draft.transcript or ''}\n\nCorreção dela: {correction.strip()}"
    parsed, notes = parse_criteria.parse(session, combined)
    zones, questions = resolve_zones(session, parsed.zones)
    draft.transcript = combined
    draft.parsed = {**parsed.model_dump(), "zone_codes": zones}
    draft.ambiguities = list(parsed.ambiguities) + questions + notes
    session.flush()
    return draft


def stale_pending(session: Session, older_than_hours: int = 48) -> list[CriteriaDraft]:
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_hours * 3600
    return [d for d in session.scalars(select(CriteriaDraft).where(CriteriaDraft.status == STATUS_PENDING))
            if d.created_at.timestamp() < cutoff]
