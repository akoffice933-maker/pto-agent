# -*- coding: utf-8 -*-
"""Печатные формы КС-3, КС-6а, М-29 из того, что уже посчитано.

Зачем отдельный слой
--------------------
Бухгалтерия и заказчик требуют не JSON и не Markdown, а бланки: справку КС-3 к актам,
журнал КС-6а накопительно и материальный отчёт М-29 по прорабу. Сейчас эти бланки
собирают руками в Excel поверх тех же цифр, которые агент уже посчитал — и именно там
появляются расхождения, которых нет ни в одном акте.

Пять правил, без которых форма врёт
-----------------------------------
1. **Форма ничего не пересчитывает.** Количества и деньги берутся из актов и документов
   как есть. Если построчные позиции не сходятся с суммой акта — в форме появляется
   строка расхождения, а не «подогнанный» итог.
2. **Итоги разной уверенности не складываются.** Проверенные акты и акты «на проверку»
   идут в форме раздельно; в сумму с НДС попадают только проверенные, остальное —
   в «не включено» с причиной.
3. **Пустой реквизит — это «не задано», а не прочерк «-».** Заказчик, подрядчик, ИНН,
   номер договора: если их нет — в форме «не задано», а в отчёте — что запросить и у кого.
4. **Ноль не подставляется вместо неизвестного.** Нет остатка на начало — остаток на
   конец «не определён» (а не равен приходу минус расход), потому что М-29 без остатка
   на начало не балансируется, а притворяться, что балансируется, — хуже.
5. **Форма — черновик, и это видно на каждой странице** (колонтитул и водяная строка):
   юридической силы у неё нет, пока документ не подписан УКЭП или на бумаге.

Соответствие бланкам:
  * КС-3, КС-6а — Постановление Госкомстата России от 11.11.1999 № 100;
  * М-29 — Постановление Госкомстата России от 30.10.1997 № 71а.
Бланки заполняются по действующим формам; наша задача — цифры и прослеживаемость,
а не перерисовка бланка «по мотивам».
"""
from __future__ import annotations

import csv
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

STATUS_DRAFT = "черновик"
SOURCE_KS = "Постановление Госкомстата России от 11.11.1999 № 100"
SOURCE_M29 = "Постановление Госкомстата России от 30.10.1997 № 71а"

#: каким статусам акта доверяем для денежного итога; всё остальное — «на проверку»
STATUS_CHECKED = {"проверен", "подписан", "signed", "verified"}

#: тип колонки в XLSX: деньги и количества должны быть числами, иначе бухгалтерия
#: не сможет их суммировать (строка «4 168 000,00» в Excel — это текст)
COL_TYPES = {
    "ks2": ["int", "text", "text", "text", "qty", "qty", "money", "money", "text"],
    "ks3": ["text", "text", "text", "money", "money", "money", "text"],
    "ks6a": ["text", "text", "text", "money", "qty", "qty", "money"],
    "m29": ["text", "text", "qty", "qty", "qty", "qty", "qty", "qty"],
}

#: человеческие подписи итогов: в бланке не должно быть английских ключей
TOTAL_LABELS = {
    "acts_in_period": "актов за период",
    "amount_checked": "сумма проверенных актов, ₽",
    "amount_review": "сумма актов «на проверку», ₽ (в итог не входит)",
    "vat_rate": "ставка НДС",
    "vat_checked": "НДС по проверенным актам, ₽",
    "total_checked_with_vat": "итого с НДС по проверенным, ₽",
    "since_contract_start_net": "с начала договора без НДС, ₽",
    "excluded": "что не включено",
    "positions": "позиций работ",
    "amount_period_net": "стоимость за период без НДС, ₽",
    "acts_used": "акты-основания",
    "period_from": "период с",
    "period_to": "период по",
    "materials": "материалов",
    "receipt_qty": "приход, ед. (суммарно по разным единицам — не складывать)",
    "expense_qty": "расход, ед. (суммарно по разным единицам — не складывать)",
    "opening_given": "остаток на начало задан",
    "balance_defined": "остаток на конец определён",
    "rows_with_overrun": "позиций с перерасходом",
    "rows_with_saving": "позиций с экономией",
    "without_norm": "позиций без нормы",
    "unit_mismatch": "позиций с расхождением единиц",
    "expense_without_receipt": "позиций «расход без прихода»",
    "amount_journal": "по журналу без НДС, ₽",
    "amount_claim": "к предъявлению без НДС, ₽",
    "amount_blocked": "не предъявляется, ₽ (в акт не входит)",
    "vat_claim": "НДС от суммы к предъявлению, ₽",
    "total_claim_with_vat": "итого к предъявлению с НДС, ₽",
    "positions": "позиций в акте",
    "positions_fully_blocked": "позиций заблокировано полностью",
    "positions_reduced": "позиций с урезанным объёмом",
}


def totals_ru(form: dict) -> list[tuple[str, str]]:
    """Итоги с русскими подписями — для бланка; в JSON остаются машинные ключи."""
    items = []
    for key, value in form["totals"].items():
        label = TOTAL_LABELS.get(key, key)
        if isinstance(value, bool):
            value = "да" if value else "нет"
        elif key == "vat_rate" and isinstance(value, (int, float)):
            value = f"{value * 100:g} %"
        elif isinstance(value, list):
            value = ", ".join(str(v) for v in value) or "—"
        items.append((label, str(value) if value not in ("", None) else "—"))
    return items


FORMS = {
    "ks3": {
        "code": "КС-3",
        "title": "Справка о стоимости выполненных работ и затрат",
        "source": SOURCE_KS,
        "columns": ["№ акта", "Дата", "Вид работ по договору", "Стоимость за период, ₽",
                    "НДС, ₽", "Всего с НДС, ₽", "Состояние"],
    },
    "ks6a": {
        "code": "КС-6а",
        "title": "Журнал учёта выполненных работ",
        "source": SOURCE_KS,
        "columns": ["Код расценки", "Наименование работ", "Ед.", "Цена за ед., ₽",
                    "За отчётный период", "С начала договора", "Стоимость за период, ₽"],
    },
    "ks2": {
        "code": "КС-2",
        "title": "Акт о приёмке выполненных работ",
        "source": SOURCE_KS,
        "columns": ["№", "Шифр расценки", "Наименование работ", "Ед. изм.",
                    "Кол-во по журналу", "К предъявлению", "Цена, ₽",
                    "Стоимость к предъявлению, ₽", "Ограничения и флаги"],
    },
    "m29": {
        "code": "М-29",
        "title": "Отчёт о расходе материалов в строительстве",
        "source": SOURCE_M29,
        "columns": ["Материал", "Ед.", "Остаток на начало", "Приход", "Расход",
                    "Норма на объём работ", "Отклонение от нормы", "Остаток на конец"],
    },
}


# ─────────────────────────── общее ───────────────────────────
def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        return None


def _rub(value: Decimal | float | None) -> str:
    if value is None:
        return "—"
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, frac = f"{amount:.2f}".split(".")
    whole = " ".join(reversed([whole[max(i - 3, 0):i] for i in range(len(whole), 0, -3)]))
    return f"{whole},{frac}"


def _qty(value: Decimal | float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    amount = Decimal(str(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    text = f"{amount:.{digits}f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") or "0"


def _money(value) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def requisites(*, object_name: str = "", contract: str = "", customer: str = "",
               contractor: str = "", period: str = "") -> dict:
    """Реквизиты шапки бланка. Пустое — «не задано», и это попадает в список запросов."""
    fields = {
        "Объект": object_name,
        "Договор": contract,
        "Заказчик": customer,
        "Подрядчик": contractor,
        "Отчётный период": period,
    }
    return {name: {"value": value, "missing": not bool(str(value).strip())}
            for name, value in fields.items()}


def _gaps_from_requisites(req: dict, form_code: str) -> list[dict]:
    gaps = []
    for name, item in req.items():
        if item["missing"]:
            gaps.append({"what": name, "why": f"в форме {form_code} реквизит обязателен, "
                                               f"а в данных его нет",
                         "ask": f"заполнить «{name}» в договоре/шапке объекта"})
    return gaps


def _period_bounds(period: str) -> tuple[str, str]:
    """«2026-09» → (2026-09-01, 2026-09-30); ISO-дата принимается как есть."""
    period = (period or "").strip()
    if len(period) == 7 and period[4] == "-":
        year, month = int(period[:4]), int(period[5:])
        last = [31, 29 if (year % 4 == 0 and year % 100 != 0) or year % 400 == 0 else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        return f"{period}-01", f"{period}-{last:02d}"
    return period, period


def _month_label(period: str) -> str:
    months = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
              "сентябрь", "октябрь", "ноябрь", "декабрь"]
    if len(period) == 7 and period[4] == "-":
        return f"{months[int(period[5:]) - 1]} {period[:4]}"
    return period


def _load_json(path: str | Path, key: str) -> tuple[list[dict], list[str]]:
    path = Path(path)
    if not path.exists():
        return [], [f"файл не найден: {path}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [], [f"{path.name}: не JSON ({error.msg}, строка {error.lineno})"]
    items = payload.get(key) if isinstance(payload, dict) else payload
    return list(items or []), []


def _load_json_raw(path: str | Path) -> tuple[dict | list, list[str]]:
    """Прочитать JSON целиком: у `summary.json` своя структура, ключа `acts` в нём нет."""
    path = Path(path)
    if not path.exists():
        return {}, [f"файл не найден: {path}"]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except json.JSONDecodeError as error:
        return {}, [f"{path.name}: не JSON ({error.msg}, строка {error.lineno})"]


def _load_csv(path: str | Path) -> tuple[list[dict], list[str]]:
    path = Path(path)
    if not path.exists():
        return [], [f"файл не найден: {path}"]
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            with path.open(encoding=encoding, newline="") as handle:
                sample = handle.read(2048)
                handle.seek(0)
                delimiter = ";" if sample.count(";") >= sample.count(",") else ","
                return [{(k or "").strip().lower(): (v or "").strip()
                         for k, v in row.items() if k}
                        for row in csv.DictReader(handle, delimiter=delimiter)], []
        except UnicodeDecodeError:
            continue
    return [], [f"{path.name}: не читается ни в utf-8, ни в cp1251"]


def _base_row(**kwargs) -> dict:
    """Строка бланка: `cells` — как показывать человеку, `values` — как считать в Excel."""
    values = [kwargs.get("c%d" % i, "") for i in range(1, 10)]
    return {"cells": [str(v) for v in values],
            "values": values,
            "state": kwargs.get("state", "ok"), "source": kwargs.get("source", ""),
            "note": kwargs.get("note", "")}


def envelope(kind: str, *, period: str, req: dict, rows: list[dict], totals: dict,
             gaps: list[dict], warnings: list[str], sources: list[str]) -> dict:
    """Общая обёртка бланка: шапка, строки, итоги, запросы, оговорки."""
    spec = FORMS[kind]
    return {
        "schema": "pto-agent/print-form@1",
        "form": kind,
        "code": spec["code"],
        "title": spec["title"],
        "approved_by": spec["source"],
        "period": period,
        "period_label": _month_label(period) if period else "",
        "status": STATUS_DRAFT,
        "draft_notice": "ЧЕРНОВИК. Не подписано. Юридической силы не имеет.",
        "requisites": req,
        "columns": spec["columns"][: len(rows[0]["cells"]) if rows else len(spec["columns"])],
        "col_types": COL_TYPES.get(kind, ["text"] * len(spec["columns"]))[:len(spec["columns"])],
        "rows": rows,
        "totals": totals,
        "gaps": gaps,
        "warnings": warnings,
        "sources": sources,
        "built_from": "артефакты прогона; форма ничего не пересчитывает",
    }



# ─────────────────────────── КС-2 ───────────────────────────
def build_ks2(positions: list[dict], *, period_from: str, period_to: str,
              requisites_dict: dict | None = None, vat_rate=None,
              number: str = "") -> dict:
    """Акт КС-2 из позиций сверки: что предъявляем, что нет и почему.

    Позиция: `code`, `name`, `unit`, `qty_journal`, `qty_claim`, `price`,
    `restrictions` (почему объём урезан), необязательные `estimate_qty`, `closed_before`.

    Три правила:
      * предъявляем `qty_claim`, а не `qty_journal` — объём «по журналу» и «к предъявлению»
        показаны рядом, чтобы урезание было видно, а не спрятано;
      * ограниченная позиция остаётся в акте с нулём к предъявлению и причиной —
        выкинуть её значит потерять работу, которую прораб уже сделал;
      * НДС и итог с НДС считаются только от предъявляемой суммы; заблокированное
        уходит отдельным итогом и в запросы, а не в сумму акта.
    """
    period = period_to or period_from
    req = requisites_dict or requisites(period=_month_label(period[:7]) if period else "")
    if not req.get("Отчётный период", {}).get("value"):
        req = requisites(period=_month_label(period[:7]) if period else "",
                         **{k: v["value"] for k, v in req.items() if v.get("value")
                            and k != "Отчётный период"})
    code = FORMS["ks2"]["code"]
    gaps = _gaps_from_requisites(req, code)
    warnings: list[str] = []

    rows: list[dict] = []
    claim_total = Decimal("0")
    journal_total = Decimal("0")
    blocked_total = Decimal("0")
    blocked_positions: list[dict] = []
    reduced_positions: list[dict] = []  # и полностью заблокированные, и урезанные

    for index, position in enumerate(positions, start=1):
        price = _dec(position.get("price"))
        qty_journal = _dec(position.get("qty_journal")) or Decimal("0")
        qty_claim = _dec(position.get("qty_claim"))
        restrictions = [str(r) for r in (position.get("restrictions") or []) if str(r).strip()]
        note = "; ".join(restrictions)

        if qty_claim is None:
            qty_claim = Decimal("0")
            note = (note + "; " if note else "") + "количество к предъявлению не задано"
        amount_claim = (qty_claim * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) \
            if price is not None else None
        state = "ok"
        if price is None:
            state = "no_price"
            notes_add = "нет цены — стоимость не считается"
            note = (note + "; " if note else "") + notes_add
            warnings.append(f"{position.get('code')}: нет цены, строка не оценена")
            amount_claim = None
        elif qty_claim == 0 and qty_journal > 0:
            state = "blocked"
            blocked_positions.append({"code": position.get("code"), "qty": str(qty_journal),
                                      "unit": position.get("unit") or "",
                                      "amount": _money(
                                          (qty_journal * price).quantize(Decimal("0.01"),
                                                                         rounding=ROUND_HALF_UP)),
                                      "why": note or "не указана причина"})
        elif restrictions:
            state = "review"
        else:
            state = "ok"

        if amount_claim is not None:
            if state == "blocked":
                reduced_positions.append({"code": position.get("code"),
                                          "unit": position.get("unit") or "",
                                          "qty_journal": str(qty_journal),
                                          "qty_claim": "0",
                                          "amount": _money(amount_claim) if amount_claim else 0.0,
                                          "why": note})
            elif qty_claim < qty_journal:
                reduced_positions.append({"code": position.get("code"),
                                          "unit": position.get("unit") or "",
                                          "qty_journal": str(qty_journal),
                                          "qty_claim": str(qty_claim),
                                          "amount": _money(
                                              ((qty_journal - qty_claim) * price).quantize(
                                                  Decimal("0.01"), rounding=ROUND_HALF_UP)),
                                          "why": note or "объём урезан без указанной причины"})
            # «не предъявляется» — вся разница между журналом и актом, не только нули
            blocked_total += ((qty_journal - qty_claim) * price).quantize(Decimal("0.01"),
                                                                        rounding=ROUND_HALF_UP)
        if state != "blocked" and amount_claim is not None:
            claim_total += amount_claim
        journal_total += (qty_journal * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) \
            if price is not None else Decimal("0")

        rows.append(_base_row(
            c1=index, c2=str(position.get("code") or "—"),
            c3=str(position.get("name") or "не задано — см. «что нужно запросить»"),
            c4=str(position.get("unit") or "—"),
            c5=_qty(qty_journal), c6=_qty(qty_claim), c7=_rub(price),
            c8=_rub(amount_claim),
            c9=note or "",
            state=state, source=str(position.get("source") or ""), note=note))

    if not positions:
        gaps.append({"what": "позиции акта", "why": "предъявлять нечего: в сверке нет позиций "
                                                    "с объёмом за период",
                     "ask": "проверить журнал работ и смету — возможно, объёмы не разнесены"})

    rate = _dec(vat_rate)
    if rate is None:
        rate = Decimal("20")
        warnings.append("ставка НДС не задана — применена 20%; проверьте реквизит договора")
        gaps.append({"what": "ставка НДС", "why": "в данных нет ставки из договора",
                     "ask": "указать ставку НДС договора (20 %, 10 %, без НДС)"})
    else:
        rate = rate / 100 if rate > 1 else rate
    vat = (claim_total * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    for item in reduced_positions:
        gaps.append({"what": f"позиция {item['code']} на {item['amount']} ₽",
                     "why": f"не предъявляется сейчас: {item['why']}",
                     "ask": "устранить причину ограничения и включить в следующий акт"})

    totals = {
        "positions": len(rows),
        "amount_journal": _money(journal_total),
        "amount_claim": _money(claim_total),
        "amount_blocked": _money(blocked_total),
        "vat_rate": float(rate),
        "vat_claim": _money(vat),
        "total_claim_with_vat": _money(claim_total + vat),
        "positions_fully_blocked": len(blocked_positions),
        "positions_reduced": len(reduced_positions),
        "excluded": ("позиции с ограничениями в сумму акта не входят" if blocked_positions else ""),
    }
    form = envelope("ks2", period=period, req=req, rows=rows, totals=totals, gaps=gaps,
                    warnings=warnings,
                    sources=["журнал выполненных работ", "смета", "расхождения сверки"])
    if number:
        form["number"] = number
    form["period_from"] = period_from
    form["period_to"] = period_to
    return form


def ks2_from_summary(payload: dict, *, req: dict | None = None,
                     vat_rate=None, number: str = "") -> dict:
    """КС-2 из `out/summary.json` прогона `pto_agent run` (без повторной сверки)."""
    lines = payload.get("lines") or []
    positions = [{
        "code": line.get("code"), "name": line.get("name"), "unit": line.get("unit"),
        "qty_journal": line.get("qty_journal"), "qty_claim": line.get("qty_claim"),
        "price": line.get("price"), "restrictions": line.get("restrictions") or [],
        "estimate_qty": line.get("estimate_qty"), "closed_before": line.get("closed_before"),
    } for line in lines
        if ((line.get("qty_journal") or 0) > 0 or (line.get("qty_claim") or 0) > 0)
        and not line.get("unit_mismatch")]
    period = payload.get("period") or []
    period_from = str(period[0]) if len(period) > 0 else ""
    period_to = str(period[1]) if len(period) > 1 else period_from
    values = {k: v for k, v in (req or {}).items() if v}
    if not values.get("Объект") and payload.get("object"):
        values["Объект"] = payload["object"]
    return build_ks2(positions, period_from=period_from, period_to=period_to,
                     requisites_dict=_req_map(values, period_to[:7]),
                     vat_rate=vat_rate if vat_rate is not None else payload.get("vat_rate"),
                     number=number)


def ks2_from_reconciliation(rec, *, vat_rate=None, requisites_dict: dict | None = None) -> dict:
    """КС-2 напрямую из объекта сверки — чтобы старый `export_ks2` шёл тем же путём."""
    positions = []
    for plan in rec.act_plan():
        line = plan["line"]
        positions.append({
            "code": line.item.code, "name": line.item.name, "unit": line.item.unit,
            "qty_journal": line.qty_journal, "qty_claim": plan["qty_claim"],
            "price": line.item.price, "restrictions": list(plan["restrictions"]),
            "estimate_qty": line.item.qty, "closed_before": line.qty_prev_closed,
        })
    period_from, period_to = rec.period[0].isoformat(), rec.period[1].isoformat()
    return build_ks2(positions, period_from=period_from, period_to=period_to,
                     requisites_dict=requisites_dict, vat_rate=vat_rate)


# ─────────────────────────── КС-3 ───────────────────────────
def build_ks3(acts: list[dict], *, period: str, requisites_dict: dict | None = None,
              vat_rate=None) -> dict:
    """Справка КС-3 за период: только проверенные акты идут в сумму, остальные — отдельно."""
    start, end = _period_bounds(period)
    req = requisites_dict or requisites(period=_month_label(period))
    code = FORMS["ks3"]["code"]
    gaps = _gaps_from_requisites(req, code)
    rows, warnings = [], []

    checked = Decimal("0")
    review = Decimal("0")
    in_period = []
    for act in acts:
        month = str(act.get("month") or "")[:7]
        act_date = str(act.get("act_date") or "")[:10]
        if not ((start <= act_date <= end) or month == (period[:7] if len(period) == 7 else "")):
            continue
        in_period.append(act)
        amount = _dec(act.get("amount_rub"))
        status = str(act.get("status") or "").strip().lower()
        is_checked = status in STATUS_CHECKED
        if amount is None:
            warnings.append(f"{act.get('act_id')}: в акте нет суммы — строка не оценена")
            state = "no_amount"
        elif is_checked:
            checked += amount
            state = "checked"
        else:
            review += amount
            state = "review"
        # построчная сверка: сумма позиций против суммы акта
        positions = act.get("positions") or []
        pos_sum = sum((_dec(p.get("amount_rub")) or Decimal("0")) for p in positions) if positions else None
        note = ""
        if not positions:
            note = "нет построчной расшифровки"
            gaps.append({"what": f"расшифровка позиций {act.get('act_id')}",
                         "why": "в акте есть сумма, но нет строк: КС-3 нечем раскрыть",
                         "ask": "прислать КС-2 с позициями или подтвердить, что акт одностатейный"})
        elif pos_sum is not None and amount is not None and pos_sum != amount:
            note = f"сумма позиций {_rub(pos_sum)} ≠ суммы акта {_rub(amount)}"
            warnings.append(f"{act.get('act_id')}: {note} (Δ {_rub(amount - pos_sum)})")
        row_vat = None
        if state == "checked" and amount is not None and _dec(vat_rate) is not None:
            rate_now = _dec(vat_rate) or Decimal("0")
            rate_now = rate_now / 100 if rate_now > 1 else rate_now
            row_vat = (amount * rate_now).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rows.append(_base_row(c1=str(act.get("act_id") or "—"),
                              c2=act_date or "—",
                              c3=_works_label(act),
                              c4=_rub(amount) if state == "checked" else "—",
                              c5=_rub(row_vat),
                              c6=_rub(amount + row_vat) if (amount is not None and row_vat is not None) else "—",
                              c7={"checked": "проверен", "review": "на проверку",
                                  "no_amount": "нет суммы"}[state],
                              state=state,
                              source=str(act.get("release") or act.get("act_id") or ""),
                              note=note))

    if not in_period:
        gaps.append({"what": "акты за период", "why": f"за {_month_label(period)} актов нет — "
                                                       f"КС-3 не из чего собрать",
                     "ask": "указать другой период или загрузить акты"})

    rate = _dec(vat_rate)
    if rate is None:
        rate = Decimal("20")
        warnings.append("ставка НДС не задана в договоре — применена 20%; проверьте реквизит")
        gaps.append({"what": "ставка НДС", "why": "в данных нет ставки из договора",
                     "ask": "указать ставку НДС договора (20 %, 10 %, без НДС)"})
    else:
        rate = rate / 100 if rate > 1 else rate

    vat_checked = (checked * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # сумма «с начала договора» — только по проверенным актам до конца периода
    since_start = Decimal("0")
    for act in acts:
        act_date = str(act.get("act_date") or "")[:10]
        if act_date and act_date <= end and str(act.get("status") or "").lower() in STATUS_CHECKED:
            since_start += _dec(act.get("amount_rub")) or Decimal("0")

    totals = {
        "acts_in_period": len(in_period),
        "amount_checked": _money(checked),
        "amount_review": _money(review),
        "vat_rate": float(rate),
        "vat_checked": _money(vat_checked),
        "total_checked_with_vat": _money(checked + vat_checked),
        "since_contract_start_net": _money(since_start),
        "excluded": ("акты «на проверку» в сумму не включены" if review else ""),
    }
    if review:
        gaps.append({"what": "проверка актов на сумму " + _rub(review) + " ₽",
                     "why": "акт не в статусе «проверен» — в справку КС-3 не включается",
                     "ask": "закрыть замечания по акту и перевести в «проверен»"})
    return envelope("ks3", period=period, req=req, rows=rows, totals=totals, gaps=gaps,
                    warnings=warnings, sources=["акты КС-2 за период"])


def _works_label(act: dict) -> str:
    positions = act.get("positions") or []
    if not positions:
        return "работы по акту (расшифровки нет)"
    names = []
    for position in positions:
        label = position.get("name") or position.get("code") or "—"
        if label not in names:
            names.append(str(label))
    return "; ".join(names)[:160]


# ─────────────────────────── КС-6а ───────────────────────────
def build_ks6a(acts: list[dict], *, period: str, requisites_dict: dict | None = None,
               names: dict | None = None) -> dict:
    """Журнал КС-6а: накопительно по коду расценки — за период и с начала договора."""
    start, end = _period_bounds(period)
    req = requisites_dict or requisites(period=_month_label(period))
    gaps = _gaps_from_requisites(req, FORMS["ks6a"]["code"])
    warnings: list[str] = []

    def in_period(act: dict) -> bool:
        act_date = str(act.get("act_date") or "")[:10]
        month = str(act.get("month") or "")[:7]
        return (start <= act_date <= end) or (len(period) == 7 and month == period[:7])

    def before(act: dict) -> bool:
        act_date = str(act.get("act_date") or "")[:10]
        return bool(act_date) and act_date < start

    acc: dict[str, dict] = {}
    bare_acts = []
    for act in acts:
        checked = str(act.get("status") or "").lower() in STATUS_CHECKED
        if in_period(act) and not (act.get("positions") or []):
            bare_acts.append(str(act.get("act_id") or "—"))
        for position in act.get("positions") or []:
            code = str(position.get("code") or "—")
            key = code
            resolved = (position.get("name") or (names or {}).get(code) or "")
            slot = acc.setdefault(key, {"code": code, "name": resolved,
                                        "unit": position.get("unit") or "",
                                        "price": _dec(position.get("price")),
                                        "period_qty": Decimal("0"), "start_qty": Decimal("0"),
                                        "period_amount": Decimal("0"), "acts": [],
                                        "checked": True, "sources": []})
            qty = _dec(position.get("qty")) or Decimal("0")
            amount = _dec(position.get("amount_rub")) or Decimal("0")
            if in_period(act):
                slot["period_qty"] += qty
                slot["period_amount"] += amount
                slot["acts"].append(act.get("act_id"))
                slot["sources"].append(f"{act.get('act_id')} от {act.get('act_date')}")
                slot["checked"] = slot["checked"] and checked
                # сверка количества × цены с суммой строки
                price = slot["price"] or _dec(position.get("price"))
                if price and qty and _dec(position.get("amount_rub")) is not None:
                    calc = (qty * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    if abs(calc - amount) > Decimal("0.01"):
                        warnings.append(f"{act.get('act_id')}, {code}: {_qty(qty)} × "
                                        f"{_rub(price)} = {_rub(calc)} ≠ {_rub(amount)} в акте; "
                                        f"в журнал взята сумма акта")
            elif before(act) and checked:
                slot["start_qty"] += qty

    rows = []
    for slot in acc.values():
        if slot["period_qty"] == 0 and slot["period_amount"] == 0:
            continue  # в журнал за период попадают только работы с движением в периоде
        state = "checked" if slot["checked"] else "review"
        note = ""
        if not slot["acts"]:
            note = "количество только с начала договора, за период движения нет"
        rows.append(_base_row(c1=slot["code"],
                              c2=slot["name"] or "не задано — см. «что нужно запросить»",
                              c3=slot["unit"] or "—",
                              c4=_rub(slot["price"]),
                              c5=_qty(slot["period_qty"]),
                              c6=_qty(slot["start_qty"] + slot["period_qty"]),
                              c7=_rub(slot["period_amount"]),
                              state=state,
                              source="; ".join(sorted(set(slot["sources"])))[:200],
                              note=note))
    rows.sort(key=lambda row: row["cells"][0])

    if bare_acts:
        gaps.append({"what": f"расшифровка актов {', '.join(bare_acts)}",
                     "why": "в акте есть сумма, но нет позиций — в журнал КС-6а эти работы "
                            "не попали, и журнал будет меньше акта",
                     "ask": "прислать КС-2 с позициями за этот акт"})
    nameless = [slot["code"] for slot in acc.values() if not slot["name"]]
    if nameless:
        gaps.append({"what": f"наименование работ для {len(nameless)} кодов расценок",
                     "why": "в акте есть код, объём и цена, но нет наименования: журнал КС-6а "
                            "без наименования не читается",
                     "ask": "прислать справочник «код → наименование» или КС-2 с наименованиями "
                            f"(коды: {', '.join(nameless[:6])}{'…' if len(nameless) > 6 else ''})"})
    if not rows:
        gaps.append({"what": "позиции работ за период",
                     "why": f"в актах за {_month_label(period)} нет построчной расшифровки — "
                            f"журнал КС-6а не из чего собрать",
                     "ask": "прислать КС-2 с позициями (код расценки, объём, единица, цена)"})

    total_period = sum((_dec(r["cells"][6].replace(" ", "").replace(",", ".").replace("—", "0") or 0)
                        or Decimal("0")) for r in rows)
    totals = {
        "positions": len(rows),
        "amount_period_net": _money(total_period),
        "acts_used": sorted({a for slot in acc.values() for a in slot["acts"]}),
        "period_from": start,
        "period_to": end,
    }
    return envelope("ks6a", period=period, req=req, rows=rows, totals=totals, gaps=gaps,
                    warnings=warnings, sources=["акты КС-2 с позициями"])


# ─────────────────────────── М-29 ───────────────────────────
def _norm_need(norms: list[dict], works: list[dict]) -> tuple[dict[str, Decimal], list[str]]:
    """Нормативный расход на фактический объём работ: норма × количество."""
    need: dict[str, Decimal] = {}
    problems = []
    qty_by_work = {str(w.get("code") or ""): _dec(w.get("qty")) for w in works}
    for norm in norms:
        work_code = str(norm.get("work_code") or "")
        qty = qty_by_work.get(work_code)
        if qty is None:
            continue
        coefficient = _dec(norm.get("norm"))
        if coefficient is None:
            problems.append(f"норма без коэффициента: {norm.get('material_code')} / {work_code}")
            continue
        key = str(norm.get("material_code") or norm.get("material_name"))
        need[key] = need.get(key, Decimal("0")) + qty * coefficient
    return need, problems


def build_m29(purchases: list[dict], writeoffs: list[dict], *, period: str,
              norms: list[dict] | None = None, works: list[dict] | None = None,
              opening: dict | None = None,
              requisites_dict: dict | None = None) -> dict:
    """Материальный отчёт М-29: остаток на начало + приход − расход = остаток на конец.

    Остаток на начало берётся из инвентаризационной ведомости (ИНВ-3) или остатка на
    первое число. Если его нет — остаток на конец **не считается**: М-29 без начального
    остатка не балансируется, и подставлять ноль значит врать.
    """
    req = requisites_dict or requisites(period=_month_label(period))
    gaps = _gaps_from_requisites(req, FORMS["m29"]["code"])
    warnings: list[str] = []
    need, problems = _norm_need(norms or [], works or [])
    warnings.extend(problems)

    opening = {str(k): _dec(v) for k, v in (opening or {}).items()}
    unit_by_material: dict[str, str] = {}
    name_by_material: dict[str, str] = {}
    receipt: dict[str, Decimal] = {}
    receipt_docs: dict[str, list[str]] = {}
    for row in purchases:
        key = row.get("код") or row.get("code") or ""
        unit = row.get("ед.") or row.get("unit") or ""
        unit_by_material.setdefault(key, unit)
        name_by_material.setdefault(key, row.get("наименование") or row.get("name") or key)
        qty = _dec(row.get("количество") or row.get("qty"))
        if qty is None:
            warnings.append(f"приход {key}: количество не разобрано "
                            f"({row.get('количество')!r}) — строка не учтена")
            continue
        receipt[key] = receipt.get(key, Decimal("0")) + qty
        doc = row.get("документ") or row.get("document") or ""
        if doc:
            receipt_docs.setdefault(key, []).append(doc)

    expense: dict[str, Decimal] = {}
    expense_docs: dict[str, list[str]] = {}
    for row in writeoffs:
        key = row.get("код") or row.get("code") or ""
        unit = row.get("ед.") or row.get("unit") or ""
        unit_by_material.setdefault(key, unit)
        name_by_material.setdefault(key, row.get("наименование") or row.get("name") or key)
        qty = _dec(row.get("количество") or row.get("qty"))
        if qty is None:
            warnings.append(f"расход {key}: количество не разобрано — строка не учтена")
            continue
        expense[key] = expense.get(key, Decimal("0")) + qty
        doc = row.get("документ") or row.get("document") or ""
        if doc:
            expense_docs.setdefault(key, []).append(doc)

    keys = sorted(set(unit_by_material) | set(opening))
    rows = []
    for key in keys:
        unit_r = unit_by_material.get(key, "")
        want_norm = need.get(key)
        unit_n = next((n.get("unit") for n in (norms or [])
                       if str(n.get("material_code")) == key), "")
        state, note = "ok", ""
        if not unit_r:
            state = "no_unit"
            note = "единица не задана в документах"
        elif want_norm is not None and unit_n and unit_n != unit_r:
            state = "unit_mismatch"
            note = (f"единицы не совпадают: документы в «{unit_r}», норматив в «{unit_n}» — "
                    f"отклонение не считается")
            warnings.append(f"{key}: {note}")
            want_norm = None

        open_qty = opening.get(key)
        has_in, has_out = key in receipt, key in expense
        in_qty = receipt.get(key, Decimal("0"))
        out_qty = expense.get(key, Decimal("0"))
        if has_out and not has_in:
            state = "expense_without_receipt"
            note = (note + "; " if note else "") + "расход без документа прихода"
            warnings.append(f"{key}: израсходовано {_qty(out_qty)} {unit_r}, а прихода по "
                            f"документам нет — либо накладная не загружена, либо списание "
                            f"из чужого прихода")
        balance = None if open_qty is None else open_qty + in_qty - out_qty
        deviation = None if (want_norm is None or state == "no_amount") else out_qty - want_norm
        if deviation is not None and deviation != 0:
            note = (note + "; " if note else "") + (
                "перерасход против нормы" if deviation > 0 else "экономия против нормы")
        rows.append(_base_row(
            c1=name_by_material.get(key, key), c2=unit_r or "—",
            c3=_qty(open_qty),
            c4=_qty(in_qty) if has_in else "—",
            c5=_qty(out_qty) if has_out else "—",
            c6=_qty(want_norm), c7=_qty(deviation), c8=_qty(balance),
            state=state,
            source="; ".join(sorted(set(receipt_docs.get(key, []) + expense_docs.get(key, []))))[:200],
            note=note))

    if not keys:
        gaps.append({"what": "движение материалов", "why": "прихода и расхода нет — отчёт пуст",
                     "ask": "загрузить накладные (приход) и требования/М-11 (расход)"})
    if opening is None or not opening:
        gaps.append({"what": "остаток на начало периода",
                     "why": "остатка на первое число нет, поэтому остаток на конец не считается: "
                            "М-29 без начального остатка не балансируется",
                     "ask": "инвентаризационная ведомость ИНВ-3 или остаток на 01 число по складу"})

    total_in = sum(receipt.values()) if receipt else Decimal("0")
    total_out = sum(expense.values()) if expense else Decimal("0")
    over = sum((d for d in (_deviation(rows, i) for i in range(len(rows)))), Decimal("0"))
    totals = {
        "materials": len(rows),
        "receipt_qty": _qty(total_in), "expense_qty": _qty(total_out),
        "opening_given": bool(opening),
        "balance_defined": bool(opening),
        "rows_with_overrun": sum(1 for row in rows if "перерасход" in row["note"]),
        "rows_with_saving": sum(1 for row in rows if "экономия" in row["note"]),
        "without_norm": sum(1 for row in rows if row["cells"][5] == "—"),
        "unit_mismatch": sum(1 for row in rows if row["state"] == "unit_mismatch"),
        "expense_without_receipt": sum(1 for row in rows
                                       if row["state"] == "expense_without_receipt"),
    }
    if totals["without_norm"]:
        gaps.append({"what": f"норма расхода для {totals['without_norm']} материалов",
                     "why": "норматива в справочнике нет — отклонение от нормы не считается",
                     "ask": "прислать нормы расхода (ГЭСН/ФЕР) на эти материалы"})
    return envelope("m29", period=period, req=req, rows=rows, totals=totals, gaps=gaps,
                    warnings=warnings, sources=["накладные (приход)", "требования М-11 / М-29 (расход)",
                                                "нормы ГЭСН/ФЕР"])


def _deviation(rows: list[dict], index: int) -> Decimal:
    return _dec(rows[index]["cells"][6].replace(",", ".")) or Decimal("0")


# ─────────────────────────── вывод ───────────────────────────
def to_markdown(form: dict) -> str:
    lines = [f"# {form['title']} ({form['code']}) — {form['period_label'] or form['period']}", "",
             f"> **{form['draft_notice']}**", "",
             f"- форма: {form['code']}, утв. {form['approved_by']}",
             f"- собрано из: {', '.join(form['sources'])}; {form['built_from']}", ""]
    lines += ["| Реквизит | Значение |", "|---|---|"]
    for name, item in form["requisites"].items():
        lines.append(f"| {name} | {item['value'] or '**не задано**'} |")
    lines.append("")
    if form["rows"]:
        lines.append("| " + " | ".join(form["columns"]) + " |")
        lines.append("|" + "---|" * len(form["columns"]))
        for row in form["rows"]:
            lines.append("| " + " | ".join(row["cells"][:len(form["columns"])]) + " |")
    else:
        lines.append("_Строк нет: данных для формы недостаточно (см. «что нужно запросить»)._")
    lines.append("")
    lines += ["## Итоги", ""]
    for label, value in totals_ru(form):
        lines.append(f"- {label}: {value}")
    lines.append("")
    if form["warnings"]:
        lines += ["## Расхождения, которые форма не скрывает", ""]
        lines += [f"- {warning}" for warning in form["warnings"]]
        lines.append("")
    if form["gaps"]:
        lines += ["## Что нужно запросить", ""]
        lines += [f"- **{gap['what']}** — {gap['why']}; нужно: {gap['ask']}" for gap in form["gaps"]]
        lines.append("")
    lines += ["## Подписи (после проверки, не черновик)", "",
              "Подрядчик: ______________________ / ______________________",
              "Заказчик: ______________________ / ______________________", ""]
    return "\n".join(lines) + "\n"


def to_docx(form: dict, path: str | Path) -> Path:
    """Бланк в DOCX с колонтитулом «ЧЕРНОВИК» на каждой странице."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()

    section = doc.sections[0]
    header = section.header.paragraphs[0]
    header.text = form["draft_notice"]
    header.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if header.runs:
        header.runs[0].font.size = Pt(9)
        header.runs[0].font.color.rgb = RGBColor(0xC0, 0x00, 0x00)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(f"Форма {form['code']}\n{form['title']}")
    run.bold = True
    run.font.size = Pt(12)
    note = doc.add_paragraph()
    note.alignment = WD_ALIGN_PARAGRAPH.CENTER
    note.add_run(f"Утв. {form['approved_by']}").font.size = Pt(8)

    for name, item in form["requisites"].items():
        paragraph = doc.add_paragraph()
        paragraph.add_run(f"{name}: ").bold = True
        paragraph.add_run(item["value"] or "не задано")

    if form["rows"]:
        table = doc.add_table(rows=1, cols=len(form["columns"]))
        table.style = "Table Grid"
        for cell, text in zip(table.rows[0].cells, form["columns"]):
            cell.text = ""
            cell.paragraphs[0].add_run(text).bold = True
            cell.paragraphs[0].runs[0].font.size = Pt(7)
        for row in form["rows"]:
            cells = table.add_row().cells
            for cell, text in zip(cells, row["cells"][:len(form["columns"])]):
                cell.text = ""
                cell.paragraphs[0].add_run(text).font.size = Pt(7)
    else:
        doc.add_paragraph("Строк нет: данных для формы недостаточно (см. «что нужно запросить»).")

    doc.add_paragraph()
    totals = doc.add_paragraph()
    totals.add_run("Итоги: ").bold = True
    totals.add_run("; ".join(f"{label} — {value}" for label, value in totals_ru(form)))

    if form["warnings"]:
        doc.add_paragraph("Расхождения, которые форма не скрывает:").runs[0].bold = True
        for warning in form["warnings"]:
            doc.add_paragraph(f"• {warning}", style="List Bullet")
    if form["gaps"]:
        doc.add_paragraph("Что нужно запросить:").runs[0].bold = True
        for gap in form["gaps"]:
            doc.add_paragraph(f"• {gap['what']}: {gap['ask']}", style="List Bullet")

    doc.add_paragraph()
    doc.add_paragraph("Подрядчик: ______________________ / ______________________")
    doc.add_paragraph("Заказчик: ______________________ / ______________________")
    tail = doc.add_paragraph(f"Собрано автоматически из: {', '.join(form['sources'])}. "
                             f"{form['built_from']}.")
    tail.runs[0].font.size = Pt(8)
    doc.save(path)
    return path


def to_xlsx(form: dict, path: str | Path) -> Path:
    """Тот же бланк в XLSX: лист формы + лист «требует данных»."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    sheet = wb.active
    sheet.title = form["code"]

    draft = sheet.cell(row=1, column=1, value=form["draft_notice"])
    draft.font = Font(bold=True, color="C00000")
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(form["columns"]), 4))

    sheet.cell(row=2, column=1, value=f"Форма {form['code']}. {form['title']}").font = Font(bold=True, size=12)
    sheet.cell(row=3, column=1, value=f"Утв. {form['approved_by']}").font = Font(size=8)

    row_index = 5
    for name, item in form["requisites"].items():
        sheet.cell(row=row_index, column=1, value=name).font = Font(bold=True)
        cell = sheet.cell(row=row_index, column=2, value=item["value"] or "не задано")
        if item["missing"]:
            cell.fill = PatternFill("solid", fgColor="FFF2CC")
        row_index += 1

    row_index += 1
    head = row_index
    for column, title in enumerate(form["columns"], start=1):
        cell = sheet.cell(row=head, column=column, value=title)
        cell.font = Font(bold=True, size=8)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.fill = PatternFill("solid", fgColor="DDEBF7")
    row_index += 1
    types = form.get("col_types") or ["text"] * len(form["columns"])
    for row in form["rows"]:
        for column, text in enumerate(row["cells"][:len(form["columns"])], start=1):
            kind = types[column - 1] if column - 1 < len(types) else "text"
            value = (row.get("values") or row["cells"])[column - 1]
            cell = sheet.cell(row=row_index, column=column, value=_excel_value(kind, value, text))
            cell.font = Font(size=8)
            if kind == "money":
                cell.number_format = "#,##0.00"
            elif kind == "qty":
                cell.number_format = "#,##0.###"
            elif kind == "int":
                cell.number_format = "0"
            if row["state"] == "review":
                cell.fill = PatternFill("solid", fgColor="FCE4D6")
            elif row["state"] in ("unit_mismatch", "no_amount", "no_unit"):
                cell.fill = PatternFill("solid", fgColor="F8CBAD")
        if row["note"] or row["source"]:
            sheet.cell(row=row_index, column=len(form["columns"]) + 1,
                       value=f"{row['note']}".strip(" ;") +
                             (f" | источник: {row['source']}" if row["source"] else "")).font = Font(size=7)
        row_index += 1

    row_index += 1
    sheet.cell(row=row_index, column=1, value="Итоги").font = Font(bold=True)
    row_index += 1
    for label, value in totals_ru(form):
        sheet.cell(row=row_index, column=1, value=label).font = Font(size=8)
        # итог тоже числом: бухгалтерия должна иметь возможность его сложить, а не копировать текст
        number = _dec(value) if "₽" in label else None
        cell = sheet.cell(row=row_index, column=2,
                          value=float(number) if number is not None else str(value))
        cell.font = Font(size=8)
        if number is not None:
            cell.number_format = "#,##0.00"
        row_index += 1

    gaps_sheet = wb.create_sheet("требует данных")
    gaps_sheet.append(["Что", "Почему", "Что нужно"])
    for gap in form["gaps"]:
        gaps_sheet.append([gap["what"], gap["why"], gap["ask"]])
    if form["warnings"]:
        gaps_sheet.append([])
        gaps_sheet.append(["Расхождения", "", ""])
        for warning in form["warnings"]:
            gaps_sheet.append([warning, "", ""])
    for column, width in zip("ABC", (46, 60, 46)):
        gaps_sheet.column_dimensions[column].width = width
    for column in range(1, len(form["columns"]) + 2):
        sheet.column_dimensions[sheet.cell(row=head, column=column).column_letter].width = 16
    wb.save(path)
    return path


def _req_map(values: dict | None, period: str) -> dict:
    """Словарь «Объект/Договор/…» → структура шапки бланка."""
    values = dict(values or {})
    return requisites(period=values.pop("Отчётный период", "") or _month_label(period),
                      object_name=values.pop("Объект", ""),
                      contract=values.pop("Договор", ""),
                      customer=values.pop("Заказчик", ""),
                      contractor=values.pop("Подрядчик", ""))


def _acts_with_meta(path: str | Path | None) -> tuple[list[dict], dict, list[str]]:
    """Акты + шапка объекта/договора из того же файла: реквизиты не выдумываем, а берём."""
    if not path:
        return [], {}, ["не задан --acts"]
    path = Path(path)
    if not path.exists():
        return [], {}, [f"файл не найден: {path}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [], {}, [f"{path.name}: не JSON ({error.msg}, строка {error.lineno})"]
    if isinstance(payload, list):
        return list(payload), {}, []
    meta = {key: payload.get(key) for key in ("object", "contract", "customer", "contractor")
            if payload.get(key)}
    return list(payload.get("acts") or []), meta, []


def _names_from_works(path: str | Path | None) -> dict:
    """Справочник «код расценки → наименование» из перечня работ прогона."""
    if not path:
        return {}
    items, _ = _load_json(path, "works")
    return {str(item.get("code")): item.get("name") or "" for item in items if item.get("code")}


def _excel_value(kind: str, value, text: str):
    """В Excel — число, если колонка числовая и значение разобралось; иначе текст."""
    if kind == "text":
        return text
    number = _dec(value)
    if number is None and kind != "int":
        return text  # «—» остаётся прочерком, а не нулём
    if number is None:
        return text
    return float(number) if kind == "money" else (
        int(number) if kind == "int" and number == int(number) else float(number))


def build_from_files(kind: str, *, period: str, acts_path: str | None = None,
                     purchases_path: str | None = None, writeoffs_path: str | None = None,
                     norms_path: str | None = None, works_path: str | None = None,
                     opening_path: str | None = None, req: dict | None = None,
                     vat_rate=None) -> dict:
    """Собрать бланк из файлов прогона (то, что уже лежит в `data/demo` или `out/`)."""
    if kind in ("ks3", "ks6a"):
        acts, meta, problems = _acts_with_meta(acts_path)
        req = dict(req or {})
        for key, name in (("object", "Объект"), ("contract", "Договор"),
                          ("customer", "Заказчик"), ("contractor", "Подрядчик")):
            if not req.get(name) and meta.get(key):
                req[name] = meta[key]
        req_map = _req_map(req, period)
        if kind == "ks3":
            form = build_ks3(acts, period=period, requisites_dict=req_map, vat_rate=vat_rate)
        else:
            form = build_ks6a(acts, period=period, requisites_dict=req_map,
                              names=_names_from_works(works_path))
        form["warnings"] = problems + form["warnings"]
        return form

    if kind == "m29":
        purchases, errors_p = _load_csv(purchases_path) if purchases_path else ([], ["не задан --purchases"])
        writeoffs, errors_w = _load_csv(writeoffs_path) if writeoffs_path else ([], ["не задан --writeoffs"])
        norms, errors_n = _load_json(norms_path, "norms") if norms_path else ([], [])
        works, errors_k = _load_json(works_path, "works") if works_path else ([], [])
        opening, errors_o = _load_json(opening_path, "opening") if opening_path else ({}, [])
        if isinstance(opening, list):
            opening = {str(item.get("код") or item.get("code")): item.get("количество")
                       for item in opening}
        form = build_m29(purchases, writeoffs, period=period, norms=norms, works=works,
                         opening=opening or None, requisites_dict=_req_map(req, period))
        form["warnings"] = (errors_p + errors_w + errors_n + errors_k + errors_o + form["warnings"])
        return form
    if kind == "ks2":
        payload, errors = _load_json_raw(acts_path) if acts_path else ({}, ["не задан --summary"])
        if isinstance(payload, list):
            payload = {"lines": payload}
        if not payload.get("lines"):
            errors.append("в файле нет позиций акта (lines) — КС-2 не из чего собрать")
        form = ks2_from_summary(payload, req=req, vat_rate=vat_rate,
                                number=str((req or {}).get("Номер акта") or ""))
        form["warnings"] = errors + form["warnings"]
        return form
    raise ValueError(f"неизвестная форма: {kind}; есть: {', '.join(FORMS)}")


def write(form: dict, out_dir: str | Path) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{form['form']}_{form['period'].replace('-', '_') or 'без_периода'}"
    payload = out / f"{stem}.json"
    payload.write_text(json.dumps(form, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = out / f"{stem}.md"
    markdown.write_text(to_markdown(form), encoding="utf-8")
    return {"json": payload, "docx": to_docx(form, out / f"{stem}.docx"),
            "xlsx": to_xlsx(form, out / f"{stem}.xlsx"), "md": markdown}
