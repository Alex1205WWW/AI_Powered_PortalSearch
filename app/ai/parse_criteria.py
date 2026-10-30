"""Her words -> structured search criteria, with the ambiguities named rather than guessed.

This is the only place a model is allowed to interpret intent, and the contract is deliberately narrow: the
model fills a fixed schema with a closed vocabulary, and anything it is unsure about goes into `ambiguities`
so the confirmation card can *ask*. The gate for Day 6 is that a muddled note produces a visibly wrong,
correctable card - never a silent mis-parse - and that is what `ambiguities` buys.

Two things the model is explicitly NOT trusted with:
  * resolving a place name to a geo code - `geo.resolve` owns that, against the real `geo` table, and
    "Lisboa" (district or council?) is asked, not assumed;
  * inventing features - `must_have` / `nice_to_have` are validated against `domain.criteria.FEATURES`
    afterwards, and anything unrecognised is dropped and reported.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai import client as ai
from app.domain.criteria import ESTATE_TYPES, FEATURES

log = logging.getLogger(__name__)

SYSTEM = """És um assistente de uma consultora imobiliária portuguesa. Recebes uma nota dela (ditada ou
escrita) a descrever a pesquisa que quer para um comprador, e devolves os critérios estruturados.

Regras:
- Extrai apenas o que a nota diz. Não inventes nem completes com valores plausíveis.
- Preços em euros. "300" ou "300 mil" significam 300000. "1,2 milhões" significa 1200000.
- Tipologia portuguesa: T0, T1, T2... "T2 ou superior" -> typology_min=2 e typology_max=null.
  "T1 ou T2" -> typology_min=1, typology_max=2. "T1+1" conta como T1.
- zones: escreve os nomes dos locais tal como ela os disse (ex. "Lisboa", "Oeiras", "Alcântara").
  NÃO tentes adivinhar distrito ou concelho; se o nome for ambíguo, diz isso em ambiguities.
- must_have: apenas o que ela exigir. nice_to_have: o que for desejável ("de preferência", "se possível").
  Usa exclusivamente estas chaves: {features}
- estate_types: exclusivamente destas chaves: {estates}
- Se ela não disser se uma característica é obrigatória ou desejável, põe-na em nice_to_have e registra a
  dúvida em ambiguities.
- ambiguities: uma pergunta curta em português por cada coisa que ficou genuinamente em dúvida. Lista
  vazia se tudo estiver claro. É melhor perguntar do que adivinhar.
- buyer_label: o nome do comprador, se a nota o disser. Caso contrário, null."""


class ParsedCriteria(BaseModel):
    """The schema the model fills. Every field optional-by-default so a partial note is still usable."""

    buyer_label: str | None = Field(default=None, description="Nome do comprador, se mencionado")
    transaction: str = Field(default="sale", description="sale para compra, rent para arrendamento")
    estate_types: list[str] = Field(default_factory=list)
    typology_min: int | None = None
    typology_max: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    area_min: int | None = None
    zones: list[str] = Field(default_factory=list, description="Nomes de locais como ela os disse")
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list, description="Termos a excluir, texto livre")
    ambiguities: list[str] = Field(default_factory=list, description="Perguntas curtas sobre o que ficou em dúvida")


def system_prompt() -> str:
    return SYSTEM.format(features=", ".join(sorted(FEATURES)), estates=", ".join(sorted(ESTATE_TYPES)))


def sanitise(parsed: ParsedCriteria) -> tuple[ParsedCriteria, list[str]]:
    """Drop anything outside the closed vocabulary and say so, rather than letting it reach the database.

    The schema constrains shape, not values: a model can still answer `must_have: ["garagem dupla"]`. Those
    are removed here and surfaced as notes on the card, so she sees what was ignored.
    """
    notes: list[str] = []

    def keep(values: list[str], label: str) -> list[str]:
        good = [v for v in dict.fromkeys(values) if v in FEATURES]
        dropped = [v for v in dict.fromkeys(values) if v not in FEATURES]
        if dropped:
            notes.append(f"Ignorei em {label}: {', '.join(dropped)} (não é uma característica que o sistema saiba filtrar)")
        return good

    parsed.must_have = keep(parsed.must_have, "obrigatórios")
    parsed.nice_to_have = keep(parsed.nice_to_have, "desejáveis")
    overlap = set(parsed.must_have) & set(parsed.nice_to_have)
    if overlap:
        parsed.nice_to_have = [f for f in parsed.nice_to_have if f not in overlap]
    bad_estates = [t for t in parsed.estate_types if t not in ESTATE_TYPES]
    if bad_estates:
        notes.append(f"Ignorei o tipo de imóvel: {', '.join(bad_estates)}")
    parsed.estate_types = [t for t in dict.fromkeys(parsed.estate_types) if t in ESTATE_TYPES] or ["apartamento"]
    if parsed.transaction not in ("sale", "rent"):
        parsed.transaction = "sale"
    if parsed.typology_min is not None and parsed.typology_max is not None and parsed.typology_min > parsed.typology_max:
        parsed.typology_min, parsed.typology_max = parsed.typology_max, parsed.typology_min
        notes.append("Troquei a tipologia mínima com a máxima, pareciam invertidas")
    if parsed.price_min is not None and parsed.price_max is not None and parsed.price_min > parsed.price_max:
        parsed.price_min, parsed.price_max = parsed.price_max, parsed.price_min
        notes.append("Troquei o preço mínimo com o máximo, pareciam invertidos")
    return parsed, notes


def parse(session: Session, text: str, *, model: str | None = None) -> tuple[ParsedCriteria, list[str]]:
    """-> (criteria, notes). Raises `ai.AiUnavailable` / `ai.AiRefused` / `ai.BudgetExceeded`; every caller
    falls back to the structured form in the panel, which never needs a model."""
    parsed = ai.parse_structured(session, operation="parse_criteria", system=system_prompt(),
                                 user=text.strip(), output_format=ParsedCriteria, model=model)
    clean, notes = sanitise(parsed)
    log.info("criteria parsed: zones=%s rooms=%s-%s price=%s-%s ambiguities=%d",
             clean.zones, clean.typology_min, clean.typology_max, clean.price_min, clean.price_max,
             len(clean.ambiguities))
    return clean, notes
