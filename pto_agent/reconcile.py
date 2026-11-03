# -*- coding: utf-8 -*-
"""Сверка объёмов и формирование таблицы расхождений. Вся арифметика — кодом."""
from dataclasses import dataclass, field

from .matching import match_entries
from .models import (SEVERITY, ClosedVolume, Discrepancy, EstimateItem,
                     JournalEntry, MatchedLine)
from .parsers import norm_code

EPS = 1e-6


@dataclass
class Reconciliation:
    object_name: str
    period: tuple
    lines: list = field(default_factory=list)
    discrepancies: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)

    # ---- агрегаты (используются отчётом и актом) ----
    @property
    def amount_estimate(self) -> float:
        return round(sum(l.item.amount for l in self.lines if l.item), 2)

    @property
    def lines_with_qty(self) -> list:
        return [l for l in self.lines if l.item and l.qty_journal > 0 and not l.unit_mismatch]

    @property
    def amount_act(self) -> float:
        """Стоимость объёмов, предъявляемых в текущем периоде."""
        return round(sum(l.amount_act for l in self.lines_with_qty), 2)

    def act_plan(self) -> list[dict]:
        """Что реально можно предъявить по каждой позиции и почему объём урезан.

        Правила ограничения:
          * нет подтверждающих документов → позиция не предъявляется вовсе;
          * объём сверх сметы или повторное закрытие → предъявляется не более
            остатка сметы (смета − закрыто ранее).
        """
        # учитываем замечания любой критичности: отсутствие ИД блокирует предъявление,
        # даже если формально это «существенное», а не «критичное» замечание
        flagged: dict[str, set] = {}
        for d in self.discrepancies:
            if d.code:
                flagged.setdefault(d.code, set()).add(d.type)

        plan = []
        for line in self.lines_with_qty:
            types = flagged.get(line.item.code, set())
            qty = line.qty_journal
            reasons = []
            if "MISSING_BACKING_DOCS" in types:
                qty, _ = 0.0, reasons.append("нет подтверждающих документов (АОСР/исп. схема)")
            if types & {"VOLUME_OVER_ESTIMATE", "ALREADY_CLOSED"}:
                limit = max(round(line.item.qty - line.qty_prev_closed, 4), 0.0)
                if qty > limit + EPS:
                    reasons.append(f"объём ограничен остатком сметы: {limit:g} {line.item.unit}")
                    qty = limit
            plan.append({
                "line": line,
                "qty_claim": round(qty, 4),
                "amount_claim": round(qty * line.item.price, 2),
                "restrictions": reasons,
            })
        return plan

    @property
    def amount_act_safe(self) -> float:
        """Сумма, которую можно предъявить после снятия ограничений (рекомендация агента)."""
        return round(sum(p["amount_claim"] for p in self.act_plan()), 2)

    @property
    def amount_blocked(self) -> float:
        return round(self.amount_act - self.amount_act_safe, 2)

    @property
    def amount_remainder(self) -> float:
        return round(sum(l.qty_remainder * l.item.price for l in self.lines
                         if l.item and l.qty_remainder > 0), 2)

    def by_severity(self) -> dict:
        out = {"critical": 0, "major": 0, "info": 0}
        for d in self.discrepancies:
            out[d.severity] = out.get(d.severity, 0) + 1
        return out

    def has_type(self, dtype: str, code: str | None = None) -> bool:
        return any(d.type == dtype and (code is None or d.code == code)
                   for d in self.discrepancies)


def reconcile(items: list[EstimateItem], entries: list[JournalEntry],
              closed: list[ClosedVolume], object_name: str = "Объект",
              period: tuple = (None, None)) -> Reconciliation:
    rec = Reconciliation(object_name=object_name, period=period)
    rec.lines = match_entries(items, entries)
    rec.unmatched = [l for l in rec.lines if l.item is None]

    prev_by_code: dict[str, float] = {}
    for c in closed:
        key = norm_code(c.code) if c.code else ""
        prev_by_code[key] = prev_by_code.get(key, 0.0) + c.qty
    rec.lines = [l for l in rec.lines if l.item is not None]
    for line in rec.lines:
        line.qty_prev_closed = round(prev_by_code.get(norm_code(line.item.code), 0.0), 4)

    _rules(rec)
    return rec


def _add(rec, dtype, code, name, values, sources, action):
    rec.discrepancies.append(Discrepancy(
        type=dtype,
        severity=SEVERITY.get(dtype, "info"),
        code=code,
        name=name,
        values=values,
        sources=sources,
        suggested_action=action,
    ))


def _rules(rec: Reconciliation) -> None:
    for line in rec.lines:
        item = line.item
        code = item.code

        # 1. Несоответствие единиц измерения — блокирует расчёт
        if line.unit_mismatch:
            _add(rec, "UNIT_MISMATCH", code, item.name,
                 {"estimate_unit": item.unit,
                  "journal_units": sorted({e.unit for e in line.entries})},
                 [f"Журнал, строка {e.row}" for e in line.entries[:3]],
                 "Уточнить единицы измерения и пересчитать объём до включения в акт")
            continue

        # 2. Дубли записей
        if line.duplicates:
            _add(rec, "DUPLICATE", code, item.name,
                 {"duplicate_rows": line.duplicates, "qty_after_dedup": line.qty_journal},
                 [f"Журнал, строка {e.row} ({e.date:%d.%m.%Y}, {e.zone})" for e in line.entries],
                 "Подтвердить фактический объём у участка, дубли исключены из акта")

        # 3. Превышение сметного объёма / повторное закрытие
        # Приоритет: сначала проверяем, превышает ли смету сам объём по журналу
        # (работа выполнена сверх сметы), затем — не выходит ли сумма
        # «закрыто ранее + текущий объём» за смету (повторное предъявление).
        total = line.qty_prev_closed + line.qty_journal
        if line.qty_journal > item.qty + EPS:
            _add(rec, "VOLUME_OVER_ESTIMATE", code, item.name,
                 {"estimate": item.qty, "actual": line.qty_journal,
                  "delta": round(line.qty_journal - item.qty, 4),
                  "delta_pct": round(100 * (line.qty_journal - item.qty) / item.qty, 2)},
                 [f"Журнал, строки {', '.join(str(e.row) for e in line.entries)}",
                  f"Смета v1, шифр {code}"],
                 "Запросить обоснование у участка или оформить изменение сметы")
        elif total > item.qty + EPS and line.qty_prev_closed > 0:
            _add(rec, "ALREADY_CLOSED", code, item.name,
                 {"estimate": item.qty, "previously_closed": line.qty_prev_closed,
                  "current": line.qty_journal, "total": round(total, 4),
                  "excess": round(total - item.qty, 4)},
                 ["Ранее закрытые акты (КС-2)", f"Журнал, строки "
                  f"{', '.join(str(e.row) for e in line.entries)}"],
                 "Не включать превышение в акт; проверить ранее закрытые объёмы")

        # 4. Отсутствие подтверждающих документов
        if line.qty_journal > 0 and not line.has_docs:
            _add(rec, "MISSING_BACKING_DOCS", code, item.name,
                 {"qty": line.qty_journal, "amount": line.amount_act},
                 [f"Журнал, строка {e.row}" for e in line.entries],
                 "Приложить АОСР/исполнительную схему до предъявления акта")

    # 5. Работы, которых нет в смете
    for line in rec.unmatched:
        e = line.entries[0]
        _add(rec, "NOT_IN_ESTIMATE", None, e.name,
             {"qty": e.qty, "unit": e.unit, "zone": e.zone},
             [f"Журнал, строка {e.row}"],
             "Запросить расценку у сметчика; в акт не включать")
