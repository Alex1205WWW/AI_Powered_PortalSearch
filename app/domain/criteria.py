"""Vocabulary and validation for buyer criteria. Shared by the panel form, the Telegram card and the matcher."""
from __future__ import annotations

from dataclasses import dataclass, field

TRANSACTIONS = {"sale": "Comprar", "rent": "Arrendar"}
ESTATE_TYPES = {"apartamento": "Apartamento", "moradia": "Moradia", "terreno": "Terreno", "outro": "Outro"}
FEATURES = {
    "garagem": "Garagem",
    "elevador": "Elevador",
    "exterior": "Terreno / espaço exterior",
    "varanda": "Varanda",
    "terraco": "Terraço",
    "jardim": "Jardim",
    "piscina": "Piscina",
    "arrecadacao": "Arrecadação",
    "vista": "Vista (mar / rio)",
}
UNKNOWN_POLICIES = {"show": "Mostrar assinalado como 'não indicado'", "hide": "Esconder se o anúncio não indicar"}
STATES = ["novo", "analisado", "contactado", "disponivel", "indisponivel"]


@dataclass
class CriteriaInput:
    buyer_id: int
    transaction: str = "sale"
    estate_types: list[str] = field(default_factory=lambda: ["apartamento"])
    typology_min: int | None = None
    typology_max: int | None = None
    price_min: int | None = None
    price_max: int | None = None
    area_min: int | None = None
    must_have: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    zones: list[str] = field(default_factory=list)
    include_private_owner: bool = True
    include_new_developments: bool = True
    unknown_policy: str = "show"
    source_text: str | None = None
    input_channel: str = "form"

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.transaction not in TRANSACTIONS:
            errors.append("Transação inválida.")
        if not self.estate_types or any(t not in ESTATE_TYPES for t in self.estate_types):
            errors.append("Escolha pelo menos um tipo de imóvel.")
        for value, label in ((self.typology_min, "Tipologia mínima"), (self.typology_max, "Tipologia máxima")):
            if value is not None and not 0 <= value <= 12:
                errors.append(f"{label} fora do intervalo T0–T12.")
        if self.typology_min is not None and self.typology_max is not None and self.typology_min > self.typology_max:
            errors.append("Tipologia mínima maior do que a máxima.")
        for value, label in ((self.price_min, "Preço mínimo"), (self.price_max, "Preço máximo"), (self.area_min, "Área mínima")):
            if value is not None and value < 0:
                errors.append(f"{label} não pode ser negativo.")
        if self.price_min is not None and self.price_max is not None and self.price_min > self.price_max:
            errors.append("Preço mínimo maior do que o máximo.")
        unknown = [f for f in self.must_have + self.nice_to_have if f not in FEATURES]
        if unknown:
            errors.append("Características desconhecidas: " + ", ".join(unknown))
        if set(self.must_have) & set(self.nice_to_have):
            errors.append("Uma característica não pode ser obrigatória e desejável ao mesmo tempo.")
        if not self.zones:
            errors.append("Escolha pelo menos uma zona.")
        if self.unknown_policy not in UNKNOWN_POLICIES:
            errors.append("Política para dados em falta inválida.")
        return errors


def typology_label(t_min: int | None, t_max: int | None) -> str:
    if t_min is None and t_max is None:
        return "qualquer tipologia"
    if t_min is not None and t_max is None:
        return f"T{t_min} ou mais"
    if t_min is None:
        return f"até T{t_max}"
    if t_min == t_max:
        return f"T{t_min}"
    return f"T{t_min} a T{t_max}"


def price_label(p_min: int | None, p_max: int | None) -> str:
    fmt = lambda v: f"{v:,.0f} €".replace(",", ".")
    if p_min is None and p_max is None:
        return "qualquer preço"
    if p_min is None:
        return f"até {fmt(p_max)}"
    if p_max is None:
        return f"a partir de {fmt(p_min)}"
    return f"{fmt(p_min)} a {fmt(p_max)}"
