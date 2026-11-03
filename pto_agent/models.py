# -*- coding: utf-8 -*-
"""Модели данных ИИ-агента ПТО."""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class EstimateItem:
    code: str
    name: str
    unit: str
    qty: float
    price: float

    @property
    def amount(self) -> float:
        return round(self.qty * self.price, 2)


@dataclass
class JournalEntry:
    date: date
    code: str
    name: str
    unit: str
    qty: float
    zone: str
    executor: str
    doc: str
    row: int = 0


@dataclass
class ClosedVolume:
    code: str
    name: str
    unit: str
    qty: float
    act: str
    period: str


@dataclass
class MatchedLine:
    """Сопоставление записи журнала с позицией сметы."""
    item: Optional[EstimateItem]
    entries: list = field(default_factory=list)
    qty_journal: float = 0.0
    qty_prev_closed: float = 0.0
    duplicates: int = 0
    unit_mismatch: bool = False
    has_docs: bool = False
    match_kind: str = ""       # code | name | fuzzy | none
    match_score: float = 0.0

    @property
    def qty_remainder(self) -> float:
        """Остаток = смета − закрыто ранее − выполнено по журналу (не закрытое)."""
        if not self.item:
            return 0.0
        return round(self.item.qty - self.qty_prev_closed - self.qty_journal, 4)

    @property
    def qty_act(self) -> float:
        return round(self.qty_journal, 4)

    @property
    def amount_act(self) -> float:
        if not self.item:
            return 0.0
        return round(self.qty_journal * self.item.price, 2)


@dataclass
class Discrepancy:
    type: str
    severity: str
    code: Optional[str]
    name: str
    values: dict
    sources: list
    suggested_action: str

    def as_dict(self) -> dict:
        return {
            "type": self.type,
            "severity": self.severity,
            "code": self.code,
            "name": self.name,
            "values": self.values,
            "sources": self.sources,
            "suggested_action": self.suggested_action,
        }


SEVERITY = {
    "VOLUME_OVER_ESTIMATE": "critical",
    "ALREADY_CLOSED": "critical",
    "UNIT_MISMATCH": "critical",
    "NOT_IN_ESTIMATE": "critical",
    "DUPLICATE": "major",
    "MISSING_BACKING_DOCS": "major",
}
