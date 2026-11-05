# -*- coding: utf-8 -*-
"""Сверка материалов: заявка/решение по поставщику → приход (накладная) → КС-2.

Зачем отдельный слой
--------------------
Закупка (`procurement`) заканчивается решением «берём у такого-то по такой-то цене».
Дальше начинается жизнь, которая с решением расходится: привезли меньше, привезли
от другого поставщика, привезли дороже, привезли то, чего не заказывали, или
предъявили в акте материал, которого на площадке не было. Для ПТО это не мелочи:
каждый такой разрыв — либо замороженные деньги, либо риск отказа заказчика, либо
перерасход против собственного обоснования.

Три правила, без которых сверка врёт:

1. **Нет документа — нет вывода.** Материал без накладной не «наверное, пришёл»:
   он в `ORDERED_NOT_DELIVERED` с деньгами и адресатом запроса. Акт без прихода —
   отдельный класс с риском отказа, а не «списалось».
2. **Единицы важнее количества.** Если единица в накладной не совпадает с заявкой,
   количество не сравнивается вовсе (`UNIT_MISMATCH`, деньги `null`) — иначе
   0,3754 «1000 м» против 375 «м» дадут ложный вывод о недопоставке в 300 раз.
3. **КС-2 не обязана содержать материалы построчно.** В расценках ФЕР/ГЭСН материал
   часто «внутри» работы. Если в акте нет кодов ресурсов, модуль не делает вид, что
   материалов в акте нет, а ставит `act_materials: unknown` и считает только
   «заявка ↔ приход».
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

TOL = 1e-4          # допуск на округление количеств
MONEY_TOL = 0.01    # допуск на округление денег

# ─────────────────────────── классы расхождений ───────────────────────────
ORDERED_NOT_DELIVERED = "ORDERED_NOT_DELIVERED"      # решено закупить — прихода нет
PARTIAL_DELIVERY = "PARTIAL_DELIVERY"                # пришло меньше, чем решено
QTY_OVER_RECEIPT = "QTY_OVER_RECEIPT"                # пришло больше, чем решено
DELIVERED_NOT_ORDERED = "DELIVERED_NOT_ORDERED"      # приход без заявки и решения
SUPPLIER_MISMATCH = "SUPPLIER_MISMATCH"              # приход от другого поставщика
PRICE_UP = "PRICE_UP"                                # цена прихода выше цены решения
PRICE_DOWN = "PRICE_DOWN"                            # цена прихода ниже (экономия)
UNIT_MISMATCH = "UNIT_MISMATCH"                      # единицы не совпадают — не считаем
ACT_WITHOUT_RECEIPT = "ACT_WITHOUT_RECEIPT"          # предъявлено в акте больше, чем пришло
ACT_NOT_DELIVERED = "ACT_NOT_DELIVERED"              # предъявлено в акте, прихода нет вовсе
RECEIPT_NOT_IN_ACT = "RECEIPT_NOT_IN_ACT"            # пришло, но в акт не попало
ACT_MATERIALS_UNKNOWN = "ACT_MATERIALS_UNKNOWN"      # в акте нет построчных материалов

TITLES = {
    ORDERED_NOT_DELIVERED: "Заказано, но не привезено — деньги заморожены",
    PARTIAL_DELIVERY: "Привезли меньше, чем решено",
    QTY_OVER_RECEIPT: "Привезли больше, чем решено",
    DELIVERED_NOT_ORDERED: "Приход без заявки — закупка вне решения",
    SUPPLIER_MISMATCH: "Приход от другого поставщика, чем в решении",
    PRICE_UP: "Цена прихода выше цены решения — перерасход",
    PRICE_DOWN: "Цена прихода ниже цены решения — экономия",
    UNIT_MISMATCH: "Разные единицы измерения — количество не сравнивалось",
    ACT_WITHOUT_RECEIPT: "В акте больше, чем пришло — риск отказа заказчика",
    ACT_NOT_DELIVERED: "Предъявлено в акте, прихода нет вовсе",
    RECEIPT_NOT_IN_ACT: "Пришло, но в акт не предъявлено",
    ACT_MATERIALS_UNKNOWN: "В акте нет построчных материалов — сверка только по приходу",
}

# что делать человеку: класс → адресат
ACTIONS = {
    ORDERED_NOT_DELIVERED: "запрос поставщику о сроке поставки",
    PARTIAL_DELIVERY: "запрос поставщику о допоставке",
    QTY_OVER_RECEIPT: "сверка с заявкой: принимать ли излишек, кто оплачивает",
    DELIVERED_NOT_ORDERED: "бухгалтерии: основание оплаты; закупщику: кто заказал",
    SUPPLIER_MISMATCH: "закупщику: письменное согласование замены поставщика",
    PRICE_UP: "бухгалтерии: подтверждение цены; закупщику: обоснование перерасхода",
    PRICE_DOWN: "закупщику: зафиксировать экономию в журнале",
    UNIT_MISMATCH: "ПТО: привести единицы к одной базе, иначе цифры не сравниваются",
    ACT_WITHOUT_RECEIPT: "ПТО: убрать из акта или предъявить накладную",
    ACT_NOT_DELIVERED: "ПТО: убрать из акта; закупщику: срочно закрыть поставку",
    RECEIPT_NOT_IN_ACT: "ПТО: включить в следующий акт или оставить на склад",
    ACT_MATERIALS_UNKNOWN: "ПТО: получить акт с построчными материалами",
}

# деньги: как трактовать сумму расхождения
MONEY_KIND = {
    ORDERED_NOT_DELIVERED: "frozen",       # заморожено: оплачено/зарезервировано, но не пришло
    PARTIAL_DELIVERY: "frozen",
    QTY_OVER_RECEIPT: "at_risk",           # излишек: риск спора об оплате
    DELIVERED_NOT_ORDERED: "at_risk",
    SUPPLIER_MISMATCH: "none",
    PRICE_UP: "overpay",
    PRICE_DOWN: "saving",
    UNIT_MISMATCH: "none",
    ACT_WITHOUT_RECEIPT: "claim_risk",     # риск отказа в оплате акта
    ACT_NOT_DELIVERED: "claim_risk",
    RECEIPT_NOT_IN_ACT: "none",
    ACT_MATERIALS_UNKNOWN: "none",
}

PRICE_ALIASES = {
    "code": ("код", "код ресурса", "code", "шифр", "артикул"),
    "name": ("наименование", "название", "name", "ресурс", "материал"),
    "unit": ("ед", "ед.", "ед. изм.", "единица", "единица измерения", "unit", "ед.изм"),
    "qty": ("кол-во", "количество", "кол", "qty", "quantity"),
    "price": ("цена", "цена за ед", "цена за ед.", "price", "стоимость"),
    "vat_rate": ("ндс", "ставка ндс", "vat", "vat_rate"),
    "supplier": ("поставщик", "supplier", "контрагент", "продавец"),
    "document": ("документ", "накладная", "упд", "счет", "счёт", "document", "акт", "№ документа"),
    "doc_date": ("дата", "дата документа", "дата поставки", "date", "дата прихода"),
    "act_no": ("акт", "акт кс-2", "кс-2", "номер акта", "act"),
    "price_basis": ("база цены", "с ндс", "price_basis"),
}


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().lower().replace("ё", "е").split())


def _pick(row: dict, key: str):
    """Взять значение колонки по любому из алиасов (регистр и «ё» не важны)."""
    normalized = {_norm(k): v for k, v in row.items()}
    for alias in PRICE_ALIASES.get(key, ()):
        if _norm(alias) in normalized:
            return normalized[_norm(alias)]
    return None


def _num(value, digits: int | None = None) -> float | None:
    """Число из «1 234,56», «1234.56», «1 234,56 руб.»; пустое — None, а не 0."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        text = str(value).strip().replace("\u00a0", "").replace(" ", "")
        text = text.replace("₽", "").replace("руб.", "").replace("руб", "").strip()
        if not text:
            return None
        text = text.replace(",", ".")
        try:
            number = float(text)
        except ValueError:
            return None
    return round(number, digits) if digits is not None else number


def _text(value) -> str:
    return str(value or "").strip()


def _show(name: str) -> str:
    """«ООО «ЭлектроТорг»» → ООО «ЭлектроТорг»: не удваиваем кавычки."""
    name = _text(name)
    if not name:
        return "—"
    return name if "«" in name else f"«{name}»"


def _price_with_vat(price, vat_rate) -> float | None:
    if price is None:
        return None
    rate = _num(vat_rate)
    if rate is None:
        return round(float(price), 2)
    return round(float(price) * (1 + rate / 100), 2)


# ─────────────────────────── чтение источников ───────────────────────────
def read_rows(path: str | Path) -> tuple[list[dict], list[str]]:
    """Прочитать накладные/акт: xlsx или csv. Возвращает строки и проблемы чтения."""
    path = Path(path)
    if not path.exists():
        return [], [f"файл не найден: {path}"]
    problems: list[str] = []
    rows: list[dict] = []
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True)
        ws = wb.active
        raw = list(ws.iter_rows(values_only=True))
    else:
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                with open(path, encoding=encoding, newline="") as f:
                    raw = [tuple(r) for r in csv.reader(f, delimiter=_sniff(path))]
                break
            except UnicodeDecodeError:
                continue
        else:
            return [], [f"не удалось определить кодировку: {path}"]
    if not raw:
        return [], [f"файл пуст: {path}"]
    header = [str(c or "").strip() for c in raw[0]]
    for number, values in enumerate(raw[1:], start=2):
        if not any(str(v or "").strip() for v in values):
            continue
        row = {header[i] if i < len(header) else f"col{i}": values[i]
               for i in range(len(values))}
        rows.append({"_row": number, **row})
    if not rows:
        problems.append(f"{path.name}: строк с данными нет")
    return rows, problems


def _sniff(path: Path) -> str:
    try:
        with open(path, encoding="utf-8-sig") as f:
            head = f.readline()
        return ";" if head.count(";") > head.count(",") else ","
    except OSError:
        return ","


def read_receipts(path: str | Path) -> tuple[list[dict], list[str]]:
    """Приход по накладным: что реально привезли, от кого, по какой цене и документу."""
    rows, problems = read_rows(path)
    receipts: list[dict] = []
    for row in rows:
        qty = _num(_pick(row, "qty"))
        price = _num(_pick(row, "price"))
        vat = _num(_pick(row, "vat_rate"))
        receipt = {
            "code": _text(_pick(row, "code")),
            "name": _text(_pick(row, "name")),
            "unit": _text(_pick(row, "unit")),
            "qty": qty,
            "price": price,
            "vat_rate": vat,
            "price_vat": _price_with_vat(price, vat),
            "supplier": _text(_pick(row, "supplier")),
            "document": _text(_pick(row, "document")),
            "doc_date": _text(_pick(row, "doc_date")),
            "row": row.get("_row"),
            "file": Path(path).name,
        }
        missing = [field for field, ok in (
            ("количество", receipt["qty"] is not None),
            ("цена", receipt["price"] is not None),
            ("единица", bool(receipt["unit"])),
            ("документ", bool(receipt["document"])),
        ) if not ok]
        if not receipt["code"] and not receipt["name"]:
            problems.append(f"{Path(path).name}:{row.get('_row')}: нет ни кода, ни наименования — "
                            f"строка пропущена")
            continue
        if missing:
            problems.append(f"{Path(path).name}:{row.get('_row')}: не указано "
                            f"{', '.join(missing)} — строка прочитана, но сверка по ней ограничена")
        receipt["missing"] = missing
        receipts.append(receipt)
    return receipts, problems


def read_act_materials(path: str | Path) -> tuple[list[dict], list[str]]:
    """Материалы из КС-2: построчные ресурсы акта (если они там есть отдельными строками)."""
    rows, problems = read_rows(path)
    materials: list[dict] = []
    for row in rows:
        qty = _num(_pick(row, "qty"))
        price = _num(_pick(row, "price"))
        vat = _num(_pick(row, "vat_rate"))
        item = {
            "code": _text(_pick(row, "code")),
            "name": _text(_pick(row, "name")),
            "unit": _text(_pick(row, "unit")),
            "qty": qty,
            "price": price,
            "price_vat": _price_with_vat(price, vat),
            "act_no": _text(_pick(row, "act_no")) or "—",
            "row": row.get("_row"),
            "file": Path(path).name,
        }
        if not item["code"] and not item["name"]:
            problems.append(f"{Path(path).name}:{row.get('_row')}: строка без кода и наименования")
            continue
        materials.append(item)
    if not materials:
        problems.append(f"{Path(path).name}: построчных материалов в акте не найдено")
    return materials, problems


def orders_from_decision(decision: dict) -> list[dict]:
    """Заказ = зафиксированное человеком решение (не «лучший по цене», а именно выбор)."""
    orders = []
    for item in decision.get("decisions") or []:
        orders.append({
            "code": _text(item.get("code")),
            "name": _text(item.get("name")),
            "unit": _text(item.get("unit")),
            "qty": _num(item.get("qty")),
            "price_vat": _num(item.get("price_vat")),
            "price_basis": item.get("price_basis") or "",
            "supplier": _text(item.get("supplier")),
            "request_no": decision.get("request_no") or "",
            "decided_by": decision.get("by") or "",
        })
    return orders


# ─────────────────────────── сверка ───────────────────────────
def _key(item: dict) -> str:
    return item.get("code") or _norm(item.get("name"))


def _discrepancy(code: str, name: str, kind: str, *, qty=None, unit="", money=None,
                 formula="", source="", detail="", supplier="") -> dict:
    return {
        "kind": kind, "title": TITLES.get(kind, kind), "code": code, "name": name,
        "qty": qty, "unit": unit,
        "money": None if money is None else round(money, 2),
        "money_kind": MONEY_KIND.get(kind, "none"),
        "formula": formula, "source": source,
        "action": ACTIONS.get(kind, ""), "detail": detail, "supplier": supplier,
    }


def reconcile(orders: list[dict], receipts: list[dict], act_materials: list[dict] | None = None,
              *, object_name: str = "", request_no: str = "",
              act_provided: bool | None = None) -> dict:
    """Сверить заказ (решение), приход (накладные) и предъявленное в акте.

    Деньги считаются только там, где есть и количество, и цена с известным источником.
    Где цены нет — `money: null` и запись в `unpriced`, а не ноль.
    """
    # «акта нет» и «в акте нет материалов» — разные вещи: без строк акта сверять нечего,
    # и вывод «материалов в акте нет» из этого делать нельзя
    if act_provided is None:
        act_provided = bool(act_materials)
    else:
        act_provided = bool(act_provided) and bool(act_materials)

    orders_by: dict[str, list[dict]] = {}
    for order in orders:
        orders_by.setdefault(_key(order), []).append(order)
    receipts_by: dict[str, list[dict]] = {}
    for receipt in receipts:
        receipts_by.setdefault(_key(receipt), []).append(receipt)
    act_by: dict[str, list[dict]] = {}
    for item in act_materials or []:
        act_by.setdefault(_key(item), []).append(item)

    codes = list(dict.fromkeys(list(orders_by) + list(receipts_by) + list(act_by)))
    lines: list[dict] = []
    discrepancies: list[dict] = []
    unpriced: list[dict] = []

    for code in codes:
        order_items = orders_by.get(code) or []
        receipt_items = receipts_by.get(code) or []
        act_items = act_by.get(code) or []
        order = order_items[0] if order_items else {}
        receipt = receipt_items[0] if receipt_items else {}
        act = act_items[0] if act_items else {}
        name = order.get("name") or receipt.get("name") or act.get("name") or code
        unit = order.get("unit") or receipt.get("unit") or act.get("unit") or ""

        qty_ordered = _sum(order_items, "qty")
        qty_received = _sum(receipt_items, "qty")
        qty_act = _sum(act_items, "qty") if act_provided else None

        price_order = _first(order.get("price_vat"), receipt.get("price_vat"),
                            act.get("price_vat"))
        price_receipt = receipt.get("price_vat")

        units = {u for u in (order.get("unit"), receipt.get("unit"), act.get("unit")) if u}
        line = {
            "code": order.get("code") or receipt.get("code") or act.get("code"),
            "name": name, "unit": unit,
            "qty_ordered": qty_ordered, "qty_received": qty_received, "qty_act": qty_act,
            "qty_open": None, "price_order": price_order, "price_receipt": price_receipt,
            "supplier_ordered": order.get("supplier") or "",
            "supplier_received": receipt.get("supplier") or "",
            "documents": sorted({r.get("document") for r in receipt_items if r.get("document")}),
            "status": "ok",
        }

        if len(units) > 1:
            lines.append({**line, "status": "unit_mismatch"})
            discrepancies.append(_discrepancy(
                line["code"], name, UNIT_MISMATCH, qty=qty_received, unit=unit,
                detail=f"единицы: заявка «{order.get('unit') or '—'}», приход "
                       f"«{receipt.get('unit') or '—'}», акт «{act.get('unit') or '—'}»",
                source="накладная / заявка / КС-2"))
            continue

        # сколько предъявлено в акте сверх прихода — учтём ниже, чтобы не посчитать дважды
        claim_qty = 0.0
        if act_provided and act_items and qty_act is not None:
            claim_qty = max(round((qty_act or 0) - (qty_received or 0), 4), 0.0)

        # ── заявка ↔ приход
        if order_items and qty_ordered is not None:
            open_qty = round((qty_ordered or 0) - (qty_received or 0), 4)
            line["qty_open"] = open_qty
            if qty_received is None or open_qty > TOL:
                # недопоставка, которая уже предъявлена в акте, — это риск по акту (ниже),
                # а не отдельно замороженные деньги: иначе одно и то же считалось бы дважды
                missing = round(max(open_qty - claim_qty, 0.0), 4)
                money = None if price_order is None else missing * price_order
                if money is None:
                    unpriced.append({"code": line["code"], "name": name,
                                     "reason": "нет цены в решении — недопоставка не оценена"})
                if missing > TOL or price_order is None:
                    covered = (f"; {claim_qty:g} {unit} из них уже предъявлено в акте — "
                               f"учтено как риск отказа" if claim_qty else "")
                    discrepancies.append(_discrepancy(
                        line["code"], name, ORDERED_NOT_DELIVERED, qty=missing, unit=unit,
                        money=money,
                        formula=(f"{missing:g} × {price_order:g} ₽" if money is not None else ""),
                        source="решение по поставщику", supplier=order.get("supplier") or "",
                        detail=(f"заказано {qty_ordered:g} {unit}, пришло "
                                f"{0 if qty_received is None else qty_received:g} {unit}"
                                + covered)))
                line["status"] = "not_delivered"
            elif open_qty < -TOL:
                money = None if price_receipt is None else -open_qty * price_receipt
                if money is None:
                    unpriced.append({"code": line["code"], "name": name,
                                     "reason": "нет цены в накладной — излишек не оценён"})
                discrepancies.append(_discrepancy(
                    line["code"], name, QTY_OVER_RECEIPT, qty=round(-open_qty, 4), unit=unit,
                    money=money,
                    formula=(f"{-open_qty:g} × {price_receipt:g} ₽" if money is not None else ""),
                    source="накладная", supplier=receipt.get("supplier") or "",
                    detail=f"заказано {qty_ordered:g} {unit}, пришло {qty_received:g} {unit}"))
                line["status"] = "over_received"
            elif 0 < (qty_received or 0) < (qty_ordered or 0) - TOL:
                line["status"] = "partial"
            else:
                line["status"] = "delivered"

        # ── приход без заявки
        if receipt_items and not order_items:
            money = None if price_receipt is None else (qty_received or 0) * price_receipt
            if money is None:
                unpriced.append({"code": line["code"], "name": name,
                                 "reason": "нет цены в накладной — приход без заявки не оценён"})
            discrepancies.append(_discrepancy(
                line["code"], name, DELIVERED_NOT_ORDERED, qty=qty_received, unit=unit,
                money=money,
                formula=(f"{qty_received:g} × {price_receipt:g} ₽" if money is not None else ""),
                source="накладная", supplier=receipt.get("supplier") or "",
                detail=f"прихода {qty_received:g} {unit}, в решении позиции нет"))
            line["status"] = "not_ordered"

        # ── поставщик и цена прихода против решения
        if order_items and receipt_items:
            if (order.get("supplier") and receipt.get("supplier")
                    and _norm(order["supplier"]) != _norm(receipt["supplier"])):
                discrepancies.append(_discrepancy(
                    line["code"], name, SUPPLIER_MISMATCH, qty=qty_received, unit=unit,
                    detail=f"в решении {_show(order['supplier'])}, в накладной "
                           f"{_show(receipt['supplier'])}", source="накладная",
                    supplier=receipt.get("supplier") or ""))
            if price_order and price_receipt:
                diff = round(price_receipt - price_order, 2)
                if abs(diff) > MONEY_TOL:
                    kind = PRICE_UP if diff > 0 else PRICE_DOWN
                    money = abs(diff) * (qty_received or 0)
                    discrepancies.append(_discrepancy(
                        line["code"], name, kind, qty=qty_received, unit=unit, money=money,
                        formula=f"{abs(diff):g} ₽ × {qty_received or 0:g} {unit}",
                        source="накладная против решения",
                        supplier=receipt.get("supplier") or "",
                        detail=f"в решении {price_order:g} ₽, в накладной {price_receipt:g} ₽"))

        # ── приход ↔ акт
        if act_provided and qty_act is not None:
            if act_items and not receipt_items:
                money = None if act.get("price_vat") is None else (qty_act or 0) * act["price_vat"]
                if money is None:
                    unpriced.append({"code": line["code"], "name": name,
                                     "reason": "нет цены в акте — предъявленное без прихода не оценено"})
                discrepancies.append(_discrepancy(
                    line["code"], name, ACT_NOT_DELIVERED, qty=qty_act, unit=unit, money=money,
                    formula=(f"{qty_act:g} × {act['price_vat']:g} ₽" if money is not None else ""),
                    source=f"КС-2 {act.get('act_no') or ''}".strip(),
                    detail="прихода по накладным нет ни одной строки"))
                line["status"] = "act_without_receipt"
            elif act_items and receipt_items:
                delta = round(claim_qty, 4)
                if delta > TOL:
                    price = _first(act.get("price_vat"), price_receipt)
                    money = None if price is None else delta * price
                    if money is None:
                        unpriced.append({"code": line["code"], "name": name,
                                         "reason": "нет цены — превышение акта над приходом не оценено"})
                    discrepancies.append(_discrepancy(
                        line["code"], name, ACT_WITHOUT_RECEIPT, qty=delta, unit=unit, money=money,
                        formula=(f"{delta:g} × {price:g} ₽" if money is not None else ""),
                        source=f"КС-2 {act.get('act_no') or ''} против накладных".strip(),
                        detail=(f"в акте {qty_act:g} {unit}, пришло {qty_received:g} {unit}; "
                            f"накладной на эту разницу нет")))
                    line["status"] = "act_over_receipt"
                elif delta < -TOL:
                    discrepancies.append(_discrepancy(
                        line["code"], name, RECEIPT_NOT_IN_ACT, qty=round(-delta, 4), unit=unit,
                        detail=f"пришло {qty_received:g} {unit}, в акте {qty_act:g} {unit}",
                        source="накладные против КС-2"))

        lines.append(line)

    if not act_provided and codes:
        discrepancies.append(_discrepancy(
            "", "", ACT_MATERIALS_UNKNOWN,
            detail="построчных материалов в акте нет или акт не передан — сверка «приход ↔ акт» "
                   "не выполнялась; «материалов в акте нет» — не доказано",
            source="КС-2"))

    totals = _totals(discrepancies)
    status = _status(lines, act_provided, orders, receipts)
    return {
        "schema": "pto-agent/materials-reconcile@1",
        "object_name": object_name, "request_no": request_no,
        "checked_at": date.today().isoformat(),
        "status": status,
        "act_materials": "known" if act_provided else "unknown",
        "lines": lines, "discrepancies": discrepancies, "unpriced": unpriced,
        "totals": totals,
        "note": ("деньги считаются только по расхождениям с известной ценой; "
                 "позиции без цены — в unpriced, это не 0 ₽"),
    }


def _sum(items: list[dict], field: str) -> float | None:
    values = [_num(i.get(field)) for i in items]
    if not any(v is not None for v in values):
        return None
    return round(sum(v or 0 for v in values), 4)


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _totals(discrepancies: list[dict]) -> dict:
    buckets = {"frozen": 0.0, "at_risk": 0.0, "overpay": 0.0, "saving": 0.0,
               "claim_risk": 0.0}
    by_kind: dict[str, float] = {}
    for item in discrepancies:
        money = item.get("money")
        kind = item.get("money_kind")
        if money is None or kind not in buckets:
            continue
        buckets[kind] += money
        by_kind[item["kind"]] = round(by_kind.get(item["kind"], 0) + money, 2)
    at_risk = round(buckets["at_risk"] + buckets["claim_risk"], 2)
    return {
        "frozen_rub": round(buckets["frozen"], 2),
        "at_risk_rub": at_risk,
        "overpay_rub": round(buckets["overpay"], 2),
        "saving_rub": round(buckets["saving"], 2),
        "claim_risk_rub": round(buckets["claim_risk"], 2),
        "by_kind": by_kind,
        "discrepancies": len(discrepancies),
        "unpriced": 0,
    }


def _status(lines: list[dict], act_provided: bool, orders: list[dict],
            receipts: list[dict]) -> str:
    if not orders and not receipts:
        return "no_data"
    broken = {"not_delivered", "over_received", "not_ordered", "unit_mismatch",
              "act_without_receipt"}
    if any(line["status"] in broken for line in lines) or not act_provided:
        return "partial"
    return "complete"


# ─────────────────────────── выгрузки ───────────────────────────
def materials_to_markdown(report: dict) -> str:
    lines = [f"# Сверка материалов: {report.get('object_name') or 'объект не указан'}", ""]
    totals = report.get("totals") or {}
    status_title = {"complete": "всё сходится", "partial": "есть расхождения",
                    "no_data": "сверять нечего"}.get(report.get("status"), report.get("status"))
    lines += [f"- статус: **{status_title}** · заявка: {report.get('request_no') or '—'}",
              f"- материалы в акте: {report.get('act_materials')}",
              f"- заморожено: {_money(totals.get('frozen_rub'))} ₽ · "
              f"риск по деньгам: {_money(totals.get('at_risk_rub'))} ₽ · "
              f"перерасход: {_money(totals.get('overpay_rub'))} ₽ · "
              f"экономия: {_money(totals.get('saving_rub'))} ₽", ""]
    lines += ["| Код | Материал | Заказано | Пришло | В акте | Статус |",
              "|---|---|---|---|---|---|"]
    for line in report.get("lines") or []:
        lines.append(f"| {line['code'] or '—'} | {line['name'][:44]} | "
                     f"{_qty(line.get('qty_ordered'))} {line.get('unit') or ''} | "
                     f"{_qty(line.get('qty_received'))} | {_qty(line.get('qty_act'))} | "
                     f"{line['status']} |")
    if report.get("discrepancies"):
        lines += ["", "## Расхождения", ""]
        for item in report["discrepancies"]:
            head = f"**{item['title']}** — {item['code'] or '—'} {item['name'][:50]}".strip()
            lines.append(f"- {head}")
            if item.get("detail"):
                lines.append(f"  - что видно: {item['detail']}")
            if item.get("money") is not None:
                lines.append(f"  - деньги: {_money(item['money'])} ₽ "
                             f"({item.get('formula') or 'формула не указана'})")
            elif item.get("money_kind") != "none":
                lines.append("  - деньги: **не оценено** (нет цены в источнике)")
            if item.get("action"):
                lines.append(f"  - кому: {item['action']}")
    if report.get("unpriced"):
        lines += ["", "**Не оценено в деньгах:**"]
        for item in report["unpriced"]:
            lines.append(f"- {item['code'] or '—'} {item['name'][:60]}: {item['reason']}")
    lines += ["", "_Материалы в акте, которых нет в накладных, — риск отказа заказчика; "
                  "недопоставка — замороженные деньги. Суммы считаются только по позициям "
                  "с известной ценой._"]
    return "\n".join(lines) + "\n"


def export_materials_xlsx(report: dict, path: str | Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Сверка"
    ws.append(["Код", "Материал", "Ед.", "Заказано", "Пришло", "В акте", "Остаток",
               "Цена решения", "Цена прихода", "Поставщик в решении", "Приход от", "Статус"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for line in report.get("lines") or []:
        ws.append([line["code"], line["name"], line.get("unit"), line.get("qty_ordered"),
                   line.get("qty_received"), line.get("qty_act"), line.get("qty_open"),
                   line.get("price_order"), line.get("price_receipt"),
                   line.get("supplier_ordered"), line.get("supplier_received"), line["status"]])

    bad = wb.create_sheet("Расхождения")
    bad.append(["Класс", "Что значит", "Код", "Материал", "Кол-во", "Сумма, руб.",
                "Формула", "Источник", "Кому"])
    for cell in bad[1]:
        cell.font = Font(bold=True)
    for item in report.get("discrepancies") or []:
        bad.append([item["kind"], item["title"], item["code"], item["name"], item.get("qty"),
                    item.get("money"), item.get("formula"), item.get("source"), item.get("action")])

    money_ws = wb.create_sheet("Итоги")
    totals = report.get("totals") or {}
    money_ws.append(["Показатель", "Сумма, руб."])
    for cell in money_ws[1]:
        cell.font = Font(bold=True)
    for key, title in (("frozen_rub", "Заморожено (недопоставка)"),
                       ("at_risk_rub", "Под риском (излишки, акт без прихода)"),
                       ("overpay_rub", "Перерасход против решения"),
                       ("saving_rub", "Экономия против решения")):
        money_ws.append([title, totals.get(key)])

    for sheet in wb.worksheets:
        sheet.freeze_panes = "A2"
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                    cell.number_format = '# ##0.00'
        sheet.column_dimensions["B"].width = 44
    wb.save(path)
    return path


def _money(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.2f}".replace(",", "\u00a0").replace(".", ",")


def _qty(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):g}".replace(".", ",")


def write(path: str | Path, report: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
