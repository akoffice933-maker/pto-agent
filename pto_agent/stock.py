# -*- coding: utf-8 -*-
"""Складской контур: склады, партии, движения, остатки.

Зачем отдельный слой
--------------------
`materials.py` сверяет заявку с приходом и КС-2 — это контроль, а не учёт. Контроль
отвечает на вопрос «совпало ли», учёт — «сколько и по какой цене лежит на складе сейчас».
Без второго не считается ни М-29 (нет остатка на начало), ни себестоимость, ни списание
по норме: всё это приходится собирать руками в Excel.

Три регистра, из которых всё выводится
--------------------------------------
* **движение** (`move`) — единственный первичный факт: дата, документ, склад, номенклатура,
  количество, цена, партия. Движения не правятся: ошибка исправляется обратным движением.
* **партия** (`batch`) — приход создаёт партию с ценой и количеством, расход её закрывает.
  Списание идёт по FIFO: раньше пришло — раньше списалось.
* **остаток** — не хранится, а **считается** из движений. Хранимый остаток рано или поздно
  расходится с движениями, и тогда непонятно, чему верить.

Семь правил, без которых склад врёт
-----------------------------------
1. **Отрицательный остаток не проводится.** Не «покажем минус» и не «потом разберёмся»:
   движение, уводящее остаток ниже нуля, отклоняется с указанием, сколько есть и чего
   не хватает. Обойти можно только явным разрешением с причиной — и это попадает в журнал.
2. **Единица проверяется до арифметики.** Если документ в других единицах, чем справочник
   номенклатуры, движение не создаётся вовсе (`UNIT_MISMATCH`), а не пересчитывается
   «на глазок».
3. **Повторное проведение не создаёт дубль.** Тот же документ (номер + дата + склад) второй
   раз даёт «уже проведён», а не удвоенный приход — загрузка накладной дважды это норма,
   а не ошибка оператора.
4. **Давальческие материалы — отдельный контур.** Они не наши: на 003, только количественный
   учёт, списать их в затраты нельзя, вернуть — только заказчику по акту.
5. **Инвентаризация — не правка остатка, а движения.** Недостача и излишек оформляются
   движениями со ссылкой на ИНВ-3: остаток всегда равен сумме движений, и это проверяется.
6. **Партия помнит документ.** Любой остаток раскрывается до партии, партия — до накладной.
   «Откуда эти 12 м³» — вопрос с ответом, а не с пожиманием плечами.
7. **Дата — не мелочь.** Движение проводится только в открытый период; проведение «задним
   числом» помечается, и остатки на прошлые даты пересчитываются, а не «замирают».
"""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

SCHEMA = "pto-agent/stock@1"
EPS = Decimal("0.0001")

#: виды документов: сколько знает склад и как они двигают остаток
DOC_KINDS = {
    "receipt": {"title": "Приход (М-4 / ТОРГ-12 / УПД)", "sign": +1, "form": "М-4",
                "ask": "накладная поставщика с ценой"},
    "issue": {"title": "Требование-накладная (М-11)", "sign": -1, "form": "М-11",
              "ask": "требование прораба с объектом списания"},
    "limit": {"title": "Лимитно-заборная карта (М-8)", "sign": -1, "form": "М-8",
              "ask": "лимит на месяц по номенклатуре"},
    "transfer": {"title": "Перемещение (М-15)", "sign": -1, "form": "М-15",
                 "ask": "склад-получатель"},
    "writeoff": {"title": "Списание (акт)", "sign": -1, "form": "акт",
                 "ask": "причина списания и подписанный акт"},
    "return": {"title": "Возврат поставщику", "sign": -1, "form": "ТОРГ-12",
               "ask": "согласие поставщика и накладная"},
    "inventory": {"title": "Инвентаризация (ИНВ-3)", "sign": 0, "form": "ИНВ-3",
                  "ask": "ведомость с фактическим наличием"},
    "daval_receipt": {"title": "Приёмка давальческого материала (М-35)", "sign": +1,
                      "form": "М-35", "ask": "акт приёмки от заказчика"},
    "daval_return": {"title": "Возврат давальческого материала", "sign": -1,
                     "form": "М-35", "ask": "акт возврата заказчику"},
}

STATUS_OK = "проведён"
STATUS_DUPLICATE = "уже проведён"
STATUS_REJECTED = "не проведён"


# ─────────────────────────── числа и деньги ───────────────────────────
def dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        return None


def q3(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def money(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ─────────────────────────── состояние ───────────────────────────
def new_state(*, object_name: str = "", warehouses: list[dict] | None = None,
              items: list[dict] | None = None, opening: list[dict] | None = None) -> dict:
    """Пустое состояние склада: справочники + журнал движений (пока пустой)."""
    state = {
        "schema": SCHEMA,
        "object": object_name,
        "warehouses": list(warehouses or []),
        "items": list(items or []),
        "documents": [],
        "moves": [],
        "rejections": [],
        "audit": [],
    }
    if opening:
        # начальные остатки — это тоже движения, просто без документа-основания:
        # иначе остаток на старте не будет равен сумме движений
        for row in opening:
            state["moves"].append(_move(
                doc_id="сальдо начальное", kind="receipt", date=str(row.get("date") or ""),
                warehouse=row.get("warehouse") or "", item=row.get("item") or "",
                qty=dec(row.get("qty")), unit=row.get("unit") or "",
                price=dec(row.get("price")) or Decimal("0"),
                batch=f"opening:{row.get('item')}:{row.get('warehouse')}",
                note=str(row.get("note") or "сальдо начальное"), daval=False))
    return state


def load(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return new_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"{path.name}: схема {payload.get('schema')!r} не {SCHEMA!r}")
    return payload


def save(state: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _move(*, doc_id: str, kind: str, date: str, warehouse: str, item: str, qty: Decimal | None,
          unit: str, price: Decimal | None, batch: str, note: str = "", daval: bool = False,
          seq: int = 0, internal: bool = False) -> dict:
    return {"doc_id": doc_id, "kind": kind, "date": date, "warehouse": warehouse,
            "item": item, "qty": float(q3(qty) or 0), "unit": unit,
            "price": money(price), "batch": batch, "note": note, "daval": bool(daval),
            "seq": seq, "internal": bool(internal)}


# ─────────────────────────── справочники ───────────────────────────
def item_info(state: dict, item: str) -> dict:
    return next((i for i in state.get("items") or [] if i.get("code") == item), {})


def warehouse_info(state: dict, code: str) -> dict:
    return next((w for w in state.get("warehouses") or [] if w.get("code") == code), {})


def _check_refs(state: dict, doc: dict) -> list[dict]:
    """Справочники должны знать склад и номенклатуру: иначе единицы не с чем сверить."""
    problems = []
    for field in ("warehouse", "warehouse_to"):
        code = doc.get(field)
        if code and not warehouse_info(state, code):
            problems.append({"what": f"склад «{code}»", "why": "склада нет в справочнике",
                             "ask": "добавить склад в справочник или исправить код"})
    for line in doc.get("lines") or []:
        item = line.get("item")
        if not item:
            problems.append({"what": "номенклатура", "why": "в строке нет кода номенклатуры",
                             "ask": "указать код номенклатуры"})
        elif not item_info(state, item):
            problems.append({"what": f"номенклатура «{item}»",
                             "why": "позиции нет в справочнике — неясна единица измерения",
                             "ask": "добавить номенклатуру с единицей измерения"})
    return problems


# ─────────────────────────── остатки и партии ───────────────────────────
def moves_of(state: dict, *, item: str | None = None, warehouse: str | None = None,
             as_of: str | None = None, daval: bool | None = None) -> list[dict]:
    moves = state.get("moves") or []
    if item is not None:
        moves = [m for m in moves if m["item"] == item]
    if warehouse is not None:
        moves = [m for m in moves if m["warehouse"] == warehouse]
    if as_of:
        moves = [m for m in moves if (m["date"] or "") <= as_of]
    if daval is not None:
        moves = [m for m in moves if bool(m.get("daval")) == daval]
    return sorted(moves, key=lambda m: (m["date"] or "", m.get("seq") or 0))


def batches(state: dict, *, item: str | None = None, warehouse: str | None = None,
            as_of: str | None = None, include_daval: bool = True) -> list[dict]:
    """Открытые партии: что пришло, что израсходовано и по какой цене осталось."""
    result: dict[str, dict] = {}
    for move in moves_of(state, as_of=as_of):
        if item is not None and move["item"] != item:
            continue
        if warehouse is not None and move["warehouse"] != warehouse:
            continue
        if not include_daval and move.get("daval"):
            continue
        key = move.get("batch") or f"{move['doc_id']}:{move['item']}"
        slot = result.setdefault(key, {"batch": key, "item": move["item"],
                                       "warehouse": move["warehouse"], "date": move["date"],
                                       "unit": move["unit"], "price": move.get("price"),
                                       "received": Decimal("0"), "issued": Decimal("0"),
                                       "daval": bool(move.get("daval")),
                                       "documents": []})
        qty = dec(move.get("qty")) or Decimal("0")
        sign = DOC_KINDS.get(move["kind"], {}).get("sign", 0)
        if sign > 0:
            slot["received"] += qty
        elif sign < 0:
            slot["issued"] += abs(qty)
        if move.get("price") is not None and slot["price"] is None:
            slot["price"] = move["price"]
        if move["doc_id"] not in slot["documents"]:
            slot["documents"].append(move["doc_id"])
    out = []
    for slot in result.values():
        left = slot["received"] - slot["issued"]
        if slot["issued"] and not slot["received"]:
            left = -slot["issued"]  # расход без партии: показываем отдельно
        slot["left"] = q3(left)
        slot["amount_left"] = money(left * dec(slot["price"])) if slot["price"] else None
        out.append(slot)
    return sorted(out, key=lambda b: (b["date"] or "", b["item"]))


def balances(state: dict, *, as_of: str | None = None, warehouse: str | None = None,
             daval: bool | None = False) -> list[dict]:
    """Остатки «номенклатура × склад» в количестве и деньгах (по средней цене остатка)."""
    acc: dict[tuple[str, str], dict] = {}
    for move in moves_of(state, warehouse=warehouse, as_of=as_of, daval=daval):
        key = (move["item"], move["warehouse"])
        slot = acc.setdefault(key, {"item": move["item"], "warehouse": move["warehouse"],
                                    "unit": move["unit"], "qty": Decimal("0"),
                                    "amount": Decimal("0"), "daval": bool(move.get("daval"))})
        sign = DOC_KINDS.get(move["kind"], {}).get("sign", 0)
        qty = dec(move.get("qty")) or Decimal("0")
        price = dec(move.get("price")) or Decimal("0")
        slot["qty"] += qty * sign
        slot["amount"] += qty * price * sign
    out = []
    for slot in acc.values():
        avg = (slot["amount"] / slot["qty"]) if slot["qty"] else None
        out.append({"item": slot["item"], "warehouse": slot["warehouse"], "unit": slot["unit"],
                    "qty": q3(slot["qty"]), "amount": money(slot["amount"]),
                    "price": money(avg), "daval": slot["daval"],
                    "negative": slot["qty"] < -EPS})
    return sorted(out, key=lambda row: (row["warehouse"], row["item"]))


def available(state: dict, *, item: str, warehouse: str, as_of: str | None = None,
              daval: bool | None = False) -> Decimal:
    total = Decimal("0")
    for row in balances(state, as_of=as_of, warehouse=warehouse, daval=daval):
        if row["item"] == item:
            total += row["qty"]
    return q3(total) or Decimal("0")


# ─────────────────────────── проведение документов ───────────────────────────
def _doc_key(doc: dict) -> str:
    return "|".join(str(doc.get(field) or "") for field in
                    ("number", "date", "warehouse", "warehouse_to", "kind"))


def _is_posted(state: dict, doc: dict) -> bool:
    key = _doc_key(doc)
    return any(existing.get("key") == key and existing.get("status") == STATUS_OK
               for existing in state.get("documents") or [])


def _fifo_take(state: dict, *, doc: dict, line: dict, qty: Decimal, warehouse: str,
               date: str, daval: bool) -> list[dict]:
    """Списать qty из открытых партий по FIFO. Если партий не хватает — не выдумываем."""
    take = qty
    plan = []
    for batch in batches(state, item=line["item"], warehouse=warehouse, as_of=date,
                         include_daval=daval):
        if take <= EPS:
            break
        if batch["daval"] != daval:
            continue
        left = dec(batch.get("left")) or Decimal("0")
        if left <= EPS:
            continue
        piece = min(left, take)
        plan.append({"batch": batch["batch"], "qty": piece, "price": dec(batch.get("price"))})
        take -= piece
    return plan if take <= EPS else []


def post_document(state: dict, doc: dict, *, allow_negative: bool = False,
                  reason: str = "", as_of_closed: str | None = None) -> dict:
    """Провести документ. Возвращает статус, движения, ошибки — и меняет состояние.

    `allow_negative` — не «разрешить минус», а «провести с записью в журнал»: причина
    обязательна, движение попадает в `audit`, и такой остаток виден в отчёте.
    """
    kind = str(doc.get("kind") or "")
    if kind not in DOC_KINDS:
        return {"status": STATUS_REJECTED, "moves": [],
                "errors": [{"code": "UNKNOWN_KIND", "what": kind,
                            "why": f"вид документа не поддержан; есть: {', '.join(DOC_KINDS)}"}],
                "gaps": [], "warnings": []}

    if as_of_closed and (doc.get("date") or "") <= as_of_closed:
        return {"status": STATUS_REJECTED, "moves": [],
                "errors": [{"code": "CLOSED_PERIOD", "what": doc.get("number"),
                            "why": f"период закрыт по {as_of_closed}: проведение только "
                                   f"сторно в открытом периоде"}],
                "gaps": [], "warnings": []}

    if _is_posted(state, doc):
        return {"status": STATUS_DUPLICATE, "moves": [],
                "errors": [], "gaps": [],
                "warnings": [f"документ {doc.get('number')} от {doc.get('date')} уже проведён — "
                             f"повтор не создаёт движений"]}

    errors = _check_refs(state, doc)
    if errors:
        return {"status": STATUS_REJECTED, "moves": [], "errors": errors, "gaps": [],
                "warnings": []}

    date = str(doc.get("date") or "")
    warehouse = str(doc.get("warehouse") or "")
    number = str(doc.get("number") or doc.get("doc_id") or "без номера")
    sign = DOC_KINDS[kind]["sign"]
    is_daval = kind.startswith("daval_")
    moves: list[dict] = []
    warnings: list[str] = []
    gaps: list[dict] = []
    seq = len(state.get("moves") or [])

    for index, line in enumerate(doc.get("lines") or [], start=1):
        item_code = str(line.get("item") or "")
        info = item_info(state, item_code)
        unit = str(line.get("unit") or info.get("unit") or "")
        qty = dec(line.get("qty"))
        if qty is None:
            errors.append({"code": "BAD_QTY", "what": f"{number}, строка {index}",
                           "why": f"количество {line.get('qty')!r} не разобрано"})
            continue
        if unit != info.get("unit"):
            errors.append({"code": "UNIT_MISMATCH", "what": f"{item_code}",
                           "why": f"документ в «{unit}», справочник в «{info.get('unit')}» — "
                                  f"пересчёт не делаем, пока единицы не сведены"})
            gaps.append({"what": f"единица измерения {item_code}",
                         "why": "в документе и в справочнике разные единицы",
                         "ask": "свести единицы (или указать коэффициент пересчёта)"})
            continue
        price = dec(line.get("price"))
        if kind == "receipt" and price is None:
            errors.append({"code": "NO_PRICE", "what": f"{number}, строка {index}",
                           "why": "приход без цены: партия без цены не даст себестоимости"})
            continue

        if kind == "inventory":
            # инвентаризация сравнивает факт с учётом и создаёт движения на разницу
            fact = qty
            fact_price = price
            book = available(state, item=item_code, warehouse=warehouse, as_of=date, daval=is_daval)
            delta = fact - book
            if abs(delta) <= EPS:
                warnings.append(f"{item_code}: факт совпал с учётом ({q3(fact)}) — движения нет")
                continue
            if delta > 0:
                # излишек — новая партия: раньше этих материалов на складе не было
                moves.append(_move(doc_id=number, kind="receipt", date=date,
                                   warehouse=warehouse, item=item_code, qty=delta, unit=unit,
                                   price=fact_price, batch=f"{number}:{item_code}:излишек",
                                   daval=is_daval, note="излишек по ИНВ-3",
                                   seq=seq + len(moves)))
            else:
                # недостача списывается из конкретных партий, а не «из воздуха»:
                # иначе учёт теряет, из какого прихода убыло
                allocations = _fifo_take(state, doc=doc, line={**line, "item": item_code},
                                         qty=abs(delta), warehouse=warehouse, date=date,
                                         daval=is_daval)
                if not allocations:
                    errors.append({"code": "NO_BATCH", "what": item_code,
                                   "why": "недостача, но открытых партий нет — сначала приход"})
                    continue
                for allocation in allocations:
                    moves.append(_move(doc_id=number, kind="writeoff", date=date,
                                       warehouse=warehouse, item=item_code,
                                       qty=allocation["qty"], unit=unit,
                                       price=allocation["price"], batch=allocation["batch"],
                                       daval=is_daval, note="недостача по ИНВ-3",
                                       seq=seq + len(moves)))
            warnings.append(f"{item_code}: {'излишек' if delta > 0 else 'недостача'} "
                            f"{q3(abs(delta))} {unit} (учёт {q3(book)}, факт {q3(fact)})")
            continue

        if sign < 0:
            have = available(state, item=item_code, warehouse=warehouse, as_of=date, daval=is_daval)
            if qty > have + EPS:
                if not allow_negative:
                    errors.append({"code": "NEGATIVE_STOCK", "what": item_code,
                                   "why": f"нужно {q3(qty)} {unit}, на складе "
                                          f"{warehouse_info(state, warehouse).get('title', warehouse)} "
                                          f"на {date or 'дату'} — {q3(have)} {unit}",
                                   "short": float(q3(qty - have))})
                    gaps.append({"what": f"{item_code}: не хватает {q3(qty - have)} {unit}",
                                 "why": "списание больше остатка — либо приход не заведён, "
                                        "либо списание не туда",
                                 "ask": "провести приход или подтвердить отрицательный остаток "
                                        "с причиной"})
                    continue
                if not str(reason).strip():
                    errors.append({"code": "NEGATIVE_NEEDS_REASON", "what": item_code,
                                   "why": "отрицательный остаток разрешён, но причина не указана"})
                    continue
                warnings.append(f"{item_code}: списание {q3(qty)} при остатке {q3(have)} — "
                                f"проведено с разрешения, причина: {reason}")

        if kind == "receipt" or (kind == "inventory" and sign > 0):
            moves.append(_move(doc_id=number, kind="receipt", date=date, warehouse=warehouse,
                               item=item_code, qty=qty, unit=unit, price=price,
                               batch=f"{number}:{item_code}:{index}", daval=is_daval,
                               note=str(line.get("note") or ""), seq=seq + len(moves)))
        elif kind == "daval_receipt":
            moves.append(_move(doc_id=number, kind="daval_receipt", date=date,
                               warehouse=warehouse, item=item_code, qty=qty, unit=unit,
                               price=price or Decimal("0"),
                               batch=f"{number}:{item_code}:{index}", daval=True,
                               note="давальческий: на 003, не в затраты", seq=seq + len(moves)))
        elif kind == "transfer":
            target = str(doc.get("warehouse_to") or "")
            if not target:
                errors.append({"code": "NO_TARGET", "what": number,
                               "why": "перемещение без склада-получателя"})
                continue
            if not warehouse_info(state, target):
                errors.append({"code": "NO_TARGET", "what": target,
                               "why": "склада-получателя нет в справочнике"})
                continue
            allocations = _fifo_take(state, doc=doc, line=line, qty=qty, warehouse=warehouse,
                                     date=date, daval=is_daval)
            if not allocations:
                errors.append({"code": "NO_BATCH", "what": item_code,
                               "why": f"на складе нет открытой партии на {q3(qty)} {unit} — "
                                      f"перемещать нечего"})
                continue
            for allocation in allocations:
                moves.append(_move(doc_id=number, kind="transfer", date=date,
                                   warehouse=warehouse, item=item_code,
                                   qty=allocation["qty"], unit=unit, price=allocation["price"],
                                   batch=allocation["batch"], daval=is_daval,
                                   note=f"перемещение на {target}", seq=seq + len(moves)))
                # цена переезжает вместе с материалом: на приёмнике — своя партия по той же цене
                moves.append(_move(doc_id=number, kind="receipt", date=date, warehouse=target,
                                   item=item_code, qty=allocation["qty"], unit=unit,
                                   price=allocation["price"],
                                   batch=f"{number}:{item_code}:{len(moves)}", daval=is_daval,
                                   note=f"перемещение с {warehouse}", seq=seq + len(moves),
                                   internal=True))
        else:
            allocations = _fifo_take(state, doc=doc, line=line, qty=qty, warehouse=warehouse,
                                     date=date, daval=is_daval)
            if not allocations:
                # партий не хватает: без разрешения — отказ, с разрешением и причиной —
                # проводим, но недостача идёт отдельной «минусовой» партией, чтобы её видел контроль
                if not allow_negative:
                    errors.append({"code": "NO_BATCH", "what": item_code,
                                   "why": f"на складе нет открытой партии на {q3(qty)} {unit} — "
                                          f"списание «из воздуха» не проводится"})
                    gaps.append({"what": f"партия для {item_code}",
                                 "why": "нет прихода, из которого списывать",
                                 "ask": "провести приход или инвентаризацию"})
                    continue
                if not str(reason).strip():
                    errors.append({"code": "NEGATIVE_NEEDS_REASON", "what": item_code,
                                   "why": "отрицательный остаток разрешён, но причина не указана"})
                    continue
                last_price = next((b.get("price") for b in
                                   batches(state, item=item_code, warehouse=warehouse,
                                           as_of=date, include_daval=is_daval)
                                   if b.get("price")), None)
                moves.append(_move(doc_id=number, kind=kind, date=date, warehouse=warehouse,
                                   item=item_code, qty=qty, unit=unit,
                                   price=dec(last_price) if last_price else None,
                                   batch=f"минус:{number}:{item_code}", daval=is_daval,
                                   note=f"сверх остатка, причина: {reason}",
                                   seq=seq + len(moves)))
                warnings.append(f"{item_code}: списано {q3(qty)} при отсутствии партии — "
                                f"проведено с разрешения, причина: {reason}")
                continue
            for allocation in allocations:
                moves.append(_move(doc_id=number, kind=kind if kind != "daval_return"
                                   else "daval_return", date=date, warehouse=warehouse,
                                   item=item_code, qty=allocation["qty"], unit=unit,
                                   price=allocation["price"], batch=allocation["batch"],
                                   daval=is_daval, note=str(line.get("note") or ""),
                                   seq=seq + len(moves)))
            if kind == "issue" and not line.get("object"):
                gaps.append({"what": f"объект списания {item_code}",
                             "why": "требование М-11 списывает материал на объект, а объект не указан",
                             "ask": "указать объект (или захватку) в строке требования"})

    if errors:
        state.setdefault("rejections", []).append({"doc": number, "date": date, "kind": kind,
                                                   "errors": errors})
        return {"status": STATUS_REJECTED, "moves": [], "errors": errors, "gaps": gaps,
                "warnings": warnings}

    state.setdefault("moves", []).extend(moves)
    state.setdefault("documents", []).append({
        "key": _doc_key(doc), "number": number, "date": date, "kind": kind,
        "warehouse": warehouse, "warehouse_to": doc.get("warehouse_to") or "",
        "status": STATUS_OK, "moves": len(moves),
        "negative_allowed": bool(allow_negative), "reason": str(reason or ""),
    })
    if allow_negative and reason:
        state.setdefault("audit", []).append({
            "what": "разрешён отрицательный остаток", "doc": number, "date": date,
            "reason": str(reason)})
    return {"status": STATUS_OK, "moves": moves, "errors": [], "gaps": gaps,
            "warnings": warnings}


def post_many(state: dict, documents: list[dict], *, allow_negative: bool = False,
              reason: str = "", as_of_closed: str | None = None) -> dict:
    """Провести пачку документов по порядку даты; итог — что прошло, что нет и почему."""
    ordered = sorted(documents, key=lambda d: (str(d.get("date") or ""), str(d.get("number") or "")))
    results = []
    for doc in ordered:
        result = post_document(state, doc, allow_negative=allow_negative, reason=reason,
                               as_of_closed=as_of_closed)
        result["document"] = {"number": doc.get("number"), "date": doc.get("date"),
                              "kind": doc.get("kind")}
        results.append(result)
    posted = [r for r in results if r["status"] == STATUS_OK]
    return {
        "posted": len(posted),
        "duplicates": sum(1 for r in results if r["status"] == STATUS_DUPLICATE),
        "rejected": sum(1 for r in results if r["status"] == STATUS_REJECTED),
        "moves": sum(len(r["moves"]) for r in results),
        "results": results,
        "errors": [error for r in results for error in r["errors"]],
        "warnings": [w for r in results for w in r["warnings"]],
        "gaps": [gap for r in results for gap in r["gaps"]],
    }


# ─────────────────────────── контроль ───────────────────────────
def verify(state: dict) -> dict:
    """Сходится ли учёт сам с собой: остатки против движений, отрицательные, партии."""
    problems: list[dict] = []
    for row in balances(state, daval=None):
        if row["negative"]:
            problems.append({"code": "NEGATIVE_BALANCE",
                             "what": f"{row['item']} / {row['warehouse']}",
                             "why": f"остаток отрицательный: {row['qty']} {row['unit']}"})
    # сумма всех движений по номенклатуре должна совпадать с остатком по всем складам
    totals: dict[str, Decimal] = {}
    for move in state.get("moves") or []:
        sign = DOC_KINDS.get(move["kind"], {}).get("sign", 0)
        totals[move["item"]] = totals.get(move["item"], Decimal("0")) + \
            (dec(move.get("qty")) or Decimal("0")) * sign
    by_item: dict[str, Decimal] = {}
    for row in balances(state, daval=None):
        by_item[row["item"]] = by_item.get(row["item"], Decimal("0")) + row["qty"]
    for item, total in totals.items():
        if abs(total - by_item.get(item, Decimal("0"))) > EPS:
            problems.append({"code": "BALANCE_MISMATCH", "what": item,
                             "why": f"движения дают {q3(total)}, остатки {q3(by_item.get(item))}"})
    # партия не может быть израсходована больше, чем получена
    for batch in batches(state):
        if str(batch["batch"]).startswith("минус:"):
            continue  # разрешённый отрицательный остаток: виден в balances, не баг учёта
        if (dec(batch["left"]) or Decimal("0")) < -EPS:
            problems.append({"code": "BATCH_OVERRUN", "what": batch["batch"],
                             "why": f"из партии списано больше, чем пришло: {batch['left']}"})
    return {"ok": not problems, "problems": problems,
            "moves": len(state.get("moves") or []),
            "documents": len(state.get("documents") or []),
            "rejected": len(state.get("rejections") or []),
            "summary": ("учёт сходится" if not problems
                        else f"расхождений: {len(problems)}")}


# ─────────────────────────── М-29 и обмен ───────────────────────────
def m29_input(state: dict, *, period: str) -> dict:
    """Остаток на начало, приход и расход за месяц — то, что нужно форме М-29.

    Остаток на начало считается из движений до первого числа — это и есть смысл регистров:
    не «введённый остаток», а след истории.
    """
    year, month = (period[:4], period[5:7]) if len(period) == 7 else ("", "")
    start = f"{year}-{month}-01" if year else period
    end = period
    opening: dict[str, float] = {}
    for row in balances(state, as_of=start, daval=False):
        # остаток на начало = всё, что было до первого числа включительно, минус первый день
        if row["qty"]:
            opening[row["item"]] = float(row["qty"])
    first_day: dict[str, Decimal] = {}
    for move in moves_of(state, as_of=start, daval=False):
        if (move["date"] or "") == start:
            sign = DOC_KINDS.get(move["kind"], {}).get("sign", 0)
            first_day[move["item"]] = first_day.get(move["item"], Decimal("0")) + \
                (dec(move.get("qty")) or Decimal("0")) * sign
    for item, delta in first_day.items():
        value = Decimal(str(opening.get(item, 0))) - delta
        if abs(value) > EPS:
            opening[item] = float(q3(value))
        else:
            opening.pop(item, None)

    receipt: dict[str, float] = {}
    expense: dict[str, float] = {}
    for move in state.get("moves") or []:
        date = str(move.get("date") or "")
        if not (start < date <= (end if len(end) > 7 else f"{year}-{month}-31")):
            continue
        if move.get("daval"):
            continue
        if move.get("internal"):
            continue
        qty = dec(move.get("qty")) or Decimal("0")
        if move["kind"] in ("receipt",):
            receipt[move["item"]] = receipt.get(move["item"], 0.0) + float(qty)
        elif move["kind"] in ("issue", "limit", "writeoff", "return"):
            expense[move["item"]] = expense.get(move["item"], 0.0) + float(qty)
    return {"opening": opening, "receipt": receipt, "expense": expense,
            "period": period,
            "note": "приход/расход/остаток — из движений склада; перемещение между складами "
                    "в расход не входит: материал остался на объекте"}


def m29_rows(state: dict, *, period: str) -> dict:
    """Строки прихода и расхода для формы М-29 — прямо из движений склада.

    М-29 — материальный отчёт, поэтому в приход идут только приходные документы,
    а в расход — списания (требование, лимит, акт, возврат). Перемещения между складами
    в отчёт не попадают: материал остался у того же материально ответственного лица.
    """
    year, month = (period[:4], period[5:7]) if len(period) == 7 else ("", "")
    start = f"{year}-{month}-01" if year else period
    end = f"{year}-{month}-31" if year else period

    def in_period(date: str) -> bool:
        return bool(date) and start <= date <= end

    purchases, writeoffs = [], []
    for move in moves_of(state, as_of=end, daval=False):
        if not in_period(move["date"]):
            continue
        info = item_info(state, move["item"])
        row = {"код": move["item"], "наименование": info.get("title") or move["item"],
               "ед.": move["unit"], "количество": str(q3(dec(move.get("qty")) or 0)),
               "документ": move["doc_id"]}
        if move.get("internal"):
            continue  # перемещение между складами — не приход и не расход для М-29
        if move["kind"] == "receipt":
            row["цена"] = "" if move.get("price") is None else str(move["price"])
            row["ндс"] = ""
            purchases.append(row)
        elif move["kind"] in ("issue", "limit", "writeoff", "return"):
            row["причина"] = move.get("note") or ""
            writeoffs.append(row)
    # FIFO дробит списание на куски по партиям, а в М-29 строка — «материал + документ»:
    # агрегируем, иначе отчёт прораба состоит из осколков одной проводки
    purchases = _merge_rows(purchases)
    writeoffs = _merge_rows(writeoffs)
    data = m29_input(state, period=period)
    return {"purchases": purchases, "writeoffs": writeoffs,
            "opening": {k: v for k, v in data["opening"].items() if abs(v) > 0.0001},
            "period": period,
            "note": "приход/расход/остаток на начало — из движений склада, а не из файлов Excel"}


def _merge_rows(rows: list[dict]) -> list[dict]:
    """Свести строки «материал + документ» в одну (см. FIFO в M-29)."""
    merged: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row.get("код") or ""), str(row.get("документ") or ""))
        if key not in merged:
            merged[key] = dict(row)
            merged[key]["количество"] = str(q3(dec(row.get("количество")) or Decimal("0")))
            continue
        slot = merged[key]
        slot["количество"] = str(q3((dec(slot.get("количество")) or Decimal("0"))
                                    + (dec(row.get("количество")) or Decimal("0"))))
    return list(merged.values())


def to_markdown(state: dict) -> str:
    lines = [f"# Склад: {state.get('object') or 'объект не задан'}", ""]
    summary = verify(state)
    lines += [f"- движений: {summary['moves']} · документов: {summary['documents']} · "
              f"отклонено: {summary['rejected']}",
              f"- контроль: {summary['summary']}", ""]
    rows = balances(state, daval=None)
    if rows:
        lines += ["## Остатки", "", "| Склад | Номенклатура | Кол-во | Ед. | Цена | Сумма | Контур |",
                  "|---|---|---|---|---|---|---|"]
        for row in rows:
            lines.append(f"| {row['warehouse']} | {row['item']} | {q3(row['qty'])} | "
                         f"{row['unit']} | {row['price'] if row['price'] is not None else '—'} | "
                         f"{row['amount'] if row['amount'] is not None else '—'} | "
                         f"{'003 давальческий' if row['daval'] else 'собственный'} |")
        lines.append("")
    else:
        lines += ["_Остатков нет._", ""]
    return "\n".join(lines) + "\n"


def read_inventory_file(path: str | Path) -> tuple[list[dict], list[str]]:
    """ИНВ-3 из CSV: что посчитали на складе фактически.

    Колонки: `код` (или `code`), `количество` (или `qty`), необязательные `ед.`, `цена`.
    Остаток по учёту не принимается из файла — он берётся из движений, иначе
    инвентаризация сравнивала бы файл с файлом.
    """
    import csv

    path = Path(path)
    if not path.exists():
        return [], [f"файл не найден: {path}"]
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            with path.open(encoding=encoding, newline="") as handle:
                sample = handle.read(2048)
                handle.seek(0)
                delimiter = ";" if sample.count(";") >= sample.count(",") else ","
                rows, problems = [], []
                for number, raw in enumerate(csv.DictReader(handle, delimiter=delimiter), start=2):
                    row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
                    item = row.get("код") or row.get("code") or row.get("номенклатура") or ""
                    qty = dec(row.get("количество") or row.get("qty"))
                    if not item:
                        problems.append(f"строка {number}: нет кода номенклатуры")
                        continue
                    if qty is None:
                        problems.append(f"строка {number}: количество "
                                        f"{row.get('количество')!r} не разобрано")
                        continue
                    line = {"item": item, "qty": float(qty)}
                    if row.get("ед.") or row.get("unit"):
                        line["unit"] = row.get("ед.") or row.get("unit")
                    if row.get("цена") or row.get("price"):
                        line["price"] = dec(row.get("цена") or row.get("price"))
                    rows.append(line)
                return rows, problems
        except UnicodeDecodeError:
            continue
    return [], [f"{path.name}: не читается ни в utf-8, ни в cp1251"]


def demo_state(*, with_opening: bool = True) -> dict:
    """Демо-склад: два склада, приход, требование, перемещение, списание, давальческий."""
    state = new_state(
        object_name="ЖК «Северный», корпус 2",
        warehouses=[{"code": "СК-1", "title": "Центральный склад", "mol": "Ким А.П."},
                    {"code": "ПР-2", "title": "Приобъектный склад, корпус 2", "mol": "Волков С.И."}],
        items=[{"code": "Бетон B25", "title": "Бетон товарный B25", "unit": "м3",
                "group": "бетон"},
               {"code": "Арматура A500", "title": "Арматура A500C, d 12 мм", "unit": "т",
                "group": "металл"},
               {"code": "Керамзит", "title": "Керамзит фракция 10-20", "unit": "м3",
                "group": "сыпучие"},
               {"code": "Труба ПВХ 110", "title": "Труба ПВХ Ø110", "unit": "м",
                "group": "трубы"}],
        opening=([{"date": "2026-09-30", "warehouse": "СК-1", "item": "Бетон B25",
                   "qty": 15, "unit": "м3", "price": 6100, "note": "ИНВ-3 на 30.09"},
                  {"date": "2026-09-30", "warehouse": "СК-1", "item": "Керамзит",
                   "qty": 8, "unit": "м3", "price": 880, "note": "ИНВ-3 на 30.09"},
                  {"date": "2026-09-30", "warehouse": "ПР-2", "item": "Труба ПВХ 110",
                   "qty": 60, "unit": "м", "price": 305, "note": "ИНВ-3 на 30.09"}]
                 if with_opening else None),
    )
    documents = [
        {"kind": "receipt", "number": "УПД №118", "date": "2026-10-01", "warehouse": "СК-1",
         "lines": [{"item": "Бетон B25", "qty": 230, "price": 6200}]},
        {"kind": "receipt", "number": "УПД №119", "date": "2026-10-02", "warehouse": "СК-1",
         "lines": [{"item": "Арматура A500", "qty": 22, "price": 68000}]},
        {"kind": "receipt", "number": "УПД №120", "date": "2026-10-03", "warehouse": "СК-1",
         "lines": [{"item": "Керамзит", "qty": 40, "price": 900},
                   {"item": "Труба ПВХ 110", "qty": 200, "price": 310}]},
        {"kind": "issue", "number": "М-11 №4", "date": "2026-10-05", "warehouse": "СК-1",
         "lines": [{"item": "Бетон B25", "qty": 210, "object": "корпус 2, плита"},
                   {"item": "Арматура A500", "qty": 19.5, "object": "корпус 2, каркас"}]},
        {"kind": "transfer", "number": "М-15 №2", "date": "2026-10-06", "warehouse": "СК-1",
         "warehouse_to": "ПР-2", "lines": [{"item": "Труба ПВХ 110", "qty": 120}]},
        {"kind": "writeoff", "number": "акт списания №1", "date": "2026-10-20",
         "warehouse": "СК-1", "lines": [{"item": "Керамзит", "qty": 12,
                                         "note": "потери при подаче"}]},
    ]
    post_many(state, documents)
    return state
