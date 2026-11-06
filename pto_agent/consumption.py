# -*- coding: utf-8 -*-
"""Материалы в цифрах: потребность по норме, списание, лимит закупки, давальческие.

Почему отдельный модуль от `materials.py`: там сверка **прихода с актом** (привезли против
предъявленного), а здесь — **расход против нормы**: сколько материала должно уйти на объём по
норме, сколько списали, уложились ли в лимит закупки и что из этого — материал заказчика.

Правила, на которых держится расчёт (все — проверяемые, а не «по опыту»):

* **норма только с источником.** Норма расхода без документа, пункта и редакции не применяется:
  строка попадает в `norms_without_source`, потребность по ней не считается. Норма с
  источником сверяется с нормативной базой агента; если документа в базе нет — расчёт помечается
  «справочно» и в подтверждённый итог не входит;
* **потребность = объём × норма.** Объём берётся из ведомости объёмов или сметы, коэффициент
  потерь — только если он указан в самой норме (иначе он не появляется);
* **перерасход без причины — ошибка.** Списание больше нормы допустимо (бой, отходы, замена),
  но только с причиной; без причины это находка, а не «списание по факту»;
* **списание без нормы не проверяется вообще** — так и пишется, а не сравнивается с нулём;
* **лимит — это потребность, а не «сколько закупили в прошлый раз».** Факт закупки против
  лимита; нет цены — лимит в деньгах не считается (не ноль рублей);
* **давальческий материал нельзя списать как свой.** Если код есть и в давальческих, и в
  собственных закупках — это двойной счёт, ошибка с обеими цифрами.

Ничего не «улучшается» молча: все расхождения идут списком проверок с кодом, уровнем и числами.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .materials import _norm, _num, _pick, _text, read_rows
from .numeric import parse_number

SCHEMA = "pto-agent/consumption@1"
TOL = 0.001

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

CHECK_NORM_NO_SOURCE = "NORM_NO_SOURCE"
CHECK_NORM_UNVERIFIED = "NORM_UNVERIFIED"
CHECK_NO_NORM = "NO_NORM"
CHECK_BAD_NUMBER = "BAD_NUMBER"
CHECK_OVER_NO_REASON = "OVER_WITHOUT_REASON"
CHECK_OVER_WITH_REASON = "OVER_WITH_REASON"
CHECK_WRITE_OFF_NO_NORM = "WRITE_OFF_WITHOUT_NORM"
CHECK_NOT_WRITTEN_OFF = "NOT_WRITTEN_OFF"
CHECK_UNIT = "UNIT_MISMATCH"
CHECK_NO_PRICE = "NO_PRICE"
CHECK_OVER_LIMIT = "OVER_LIMIT"
CHECK_PURCHASE_NO_NEED = "PURCHASE_WITHOUT_NEED"
CHECK_DV_NEGATIVE = "DAVAL_NEGATIVE_BALANCE"
CHECK_DV_DOUBLE = "DAVAL_DOUBLE_ACCOUNT"
CHECK_DV_REMAINDER = "DAVAL_REMAINDER_NOT_RETURNED"


def _rub(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _qty(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")
    return text.rstrip("0").rstrip(",") if "," in text else text


# ─────────────────────────── чтение входных данных ───────────────────────────
def load_json(path: str | Path, key: str) -> tuple[list[dict], list[str]]:
    """Прочитать список из JSON: либо массив, либо объект с ключом (`works`, `norms`, `items`)."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [], [f"{path.name}: не читается ({exc.__class__.__name__})"]
    if isinstance(data, dict):
        data = data.get(key) or data.get("items") or data.get("rows") or []
    if not isinstance(data, list):
        return [], [f"{path.name}: ожидался список (ключ «{key}»)"]
    return data, []


def load_table(path: str | Path, kind: str) -> tuple[list[dict], list[str]]:
    """Прочитать xlsx/csv: списание, закупки или давальческие (колонки по алиасам)."""
    rows, problems = read_rows(path)
    items: list[dict] = []
    for row in rows:
        item = {
            "code": _text(_pick_ext(row, "code")),
            "name": _text(_pick_ext(row, "name")),
            "unit": _text(_pick_ext(row, "unit")),
            "row": row.get("_row"),
            "file": Path(path).name,
        }
        if kind == "writeoff":
            item["qty"] = parse_number(_pick_ext(row, "qty"))
            item["document"] = _text(_pick_ext(row, "document")) or None
            item["reason"] = _text(_pick_ext(row, "reason")) or None
        elif kind == "purchase":
            item["qty"] = parse_number(_pick_ext(row, "qty"))
            item["price"] = parse_number(_pick_ext(row, "price"))
            item["vat_rate"] = parse_number(_pick_ext(row, "vat_rate"))
            item["document"] = _text(_pick_ext(row, "document")) or None
        elif kind == "daval":
            item["received"] = parse_number(_pick_ext(row, "received"))
            item["used"] = parse_number(_pick_ext(row, "used"))
            item["returned"] = parse_number(_pick_ext(row, "returned"))
            item["document"] = _text(_pick_ext(row, "document")) or None
        if not item["code"] and not item["name"]:
            problems.append(f"{Path(path).name}:{row.get('_row')}: строка без кода и "
                            f"наименования — пропущена")
            continue
        items.append(item)
    return items, problems


# колонки, которых нет в общих алиасах `materials` (там приход и акт, здесь — расход и давальческие)
EXTRA_ALIASES = {
    "reason": ("причина", "обоснование", "основание", "reason", "пояснение"),
    "received": ("получено", "получено от заказчика", "приход от заказчика", "received"),
    "used": ("израсходовано", "израсходовано на объекте", "расход", "used"),
    "returned": ("возвращено", "возврат", "возвращено заказчику", "returned"),
}


def _pick_ext(row: dict, key: str):
    """Значение колонки по общим алиасам `materials` и по своим (причина, получено/израсходовано)."""
    normalized = {_norm(k): v for k, v in row.items()}
    for alias in EXTRA_ALIASES.get(key, ()):
        if _norm(alias) in normalized:
            return normalized[_norm(alias)]
    return _pick(row, key)


def _key(item: dict) -> str:
    return _norm(item.get("code")) or _norm(item.get("name"))


# ─────────────────────────── нормы ───────────────────────────
def verify_sources(norms: list[dict], folder=None) -> dict:
    """Сверить источники норм с нормативной базой агента."""
    from .asbuilt.norms_check import verify_norms

    refs = [{"doc": str((n.get("source") or {}).get("doc") or ""),
             "clause": str((n.get("source") or {}).get("point") or "")} for n in norms]
    return verify_norms(refs, folder)


def prepare_norms(norms: list[dict], folder=None) -> tuple[list[dict], list[dict], list[dict]]:
    """Разложить нормы: применимые, без источника, с неподтверждённым источником.

    Возвращает (нормы с расчётным статусом, без источника, источник не подтверждён базой).
    """
    rows: list[dict] = []
    no_source: list[dict] = []
    for norm in norms:
        source = norm.get("source") or {}
        doc = str(source.get("doc") or "").strip()
        point = str(source.get("point") or "").strip()
        edition = str(source.get("edition") or "").strip()
        record = {
            "work_code": _text(norm.get("work_code")),
            "work_name": _text(norm.get("work_name")),
            "material_code": _text(norm.get("material_code") or norm.get("code")),
            "material_name": _text(norm.get("material_name") or norm.get("name")),
            "unit": _text(norm.get("unit")),
            "norm": parse_number(norm.get("norm")),
            "loss_pct": parse_number(norm.get("loss_pct")),
            "price": parse_number(norm.get("price")),
            "source": {"doc": doc, "point": point, "edition": edition},
        }
        missing = [name for name, ok in (("документ", bool(doc)), ("пункт", bool(point)),
                                         ("редакция", bool(edition))) if not ok]
        record["norm_value"] = parse_number(norm.get("norm"))
        if record["norm_value"] is None:
            record["status"] = "bad_number"
        elif missing:
            record["status"] = "no_source"
            record["why"] = f"в источнике нормы не указано: {', '.join(missing)}"
            no_source.append(record)
            continue
        else:
            record["status"] = "ok"
        rows.append(record)

    verified = verify_sources([r for r in rows if r["status"] == "ok"], folder)
    for record, result in zip([r for r in rows if r["status"] == "ok"], verified["results"]):
        record["source_status"] = result["status"]
        record["source_note"] = result["note"]
        record["source_verified"] = result["status"] in ("verified", "verified_partial")
    unverified = [r for r in rows if r["status"] == "ok" and not r.get("source_verified")]
    return rows, no_source, unverified


# ─────────────────────────── потребность ───────────────────────────
def build_need(works: list[dict], norms: list[dict], *, folder=None,
               confirmed_by: str = "", role: str = "", confirm_norms: bool = False) -> dict:
    """Потребность = объём работ × норма. Нормы без источника не применяются.

    `confirm_norms` — человек принял нормы для этого расчёта (ФИО и роль обязательны). Тогда
    нормы с неподтверждённым базой источником входят в подтверждённый итог, но в отчёте
    остаётся запись, кто их принял: ответственность за норму не переходит к агенту.
    """
    prepared, no_source, unverified = prepare_norms(norms, folder)
    checks: list[dict] = []

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    by_work: dict[str, dict] = {}
    for work in works or []:
        wcode = _text(work.get("code") or work.get("work_code"))
        by_work.setdefault(_norm(wcode), {"code": wcode, "name": _text(work.get("name")),
                                          "unit": _text(work.get("unit")),
                                          "qty": parse_number(work.get("qty")),
                                          "source": _text(work.get("source"))})

    for record in no_source:
        add(CHECK_NORM_NO_SOURCE, LEVEL_ERROR,
            f"{record['material_code'] or record['material_name']}: {record['why']} — норма не "
            f"применяется, потребность по ней не считается",
            material_code=record["material_code"] or None)
    for record in prepared:
        if record["status"] == "bad_number":
            add(CHECK_BAD_NUMBER, LEVEL_ERROR,
                f"{record['material_code'] or record['material_name']}: норма расхода "
                f"«{record.get('norm')}» не число — строка не применяется",
                material_code=record["material_code"] or None)
        elif not record.get("source_verified"):
            accepted = confirm_norms and confirmed_by and role
            add(CHECK_NORM_UNVERIFIED, LEVEL_WARNING,
                f"{record['material_code']}: источник нормы ({record['source']['doc']}, п. "
                f"{record['source']['point']}) не подтверждён базой агента "
                f"({record.get('source_note') or 'нет статуса'}) — "
                + (f"норма принята для расчёта человеком: {confirmed_by}, {role}"
                   if accepted else
                   "расчёт справочный, в подтверждённый итог не входит"),
                material_code=record["material_code"])

    rows: dict[tuple[str, str], dict] = {}
    normed_works: set[str] = set()
    for record in prepared:
        if record["status"] != "ok":
            continue
        work = by_work.get(_norm(record["work_code"]))
        if not work:
            add(CHECK_NO_NORM, LEVEL_WARNING,
                f"норма для {record['material_code']} указана на вид работ "
                f"«{record['work_code']}», которого нет в ведомости объёмов — потребность не "
                f"считается", material_code=record["material_code"])
            continue
        normed_works.add(_norm(record["work_code"]))
        if work["qty"] is None:
            add(CHECK_BAD_NUMBER, LEVEL_ERROR,
                f"{work['code']}: объём работ «{work.get('qty')}» не число — потребность по "
                f"{record['material_code']} не считается")
            continue

        loss = record.get("loss_pct") or 0.0
        need_qty = round(work["qty"] * record["norm_value"] * (1 + loss / 100), 4)
        key = (_key({"code": record["material_code"], "name": record["material_name"]}),
               record["unit"])
        row = rows.get(key)
        if row is None:
            row = {"material_code": record["material_code"] or None,
                   "material_name": record["material_name"], "unit": record["unit"],
                   "need_qty": 0.0, "need_confirmed_qty": 0.0, "reference_qty": 0.0,
                   "price": record.get("price"), "works": [], "norms": []}
            rows[key] = row
        row["need_qty"] += need_qty
        if record.get("source_verified") or (confirm_norms and confirmed_by and role):
            row["need_confirmed_qty"] += need_qty
        else:
            row["reference_qty"] += need_qty
        row["works"].append({"work_code": work["code"], "work_qty": work["qty"],
                             "norm": record["norm_value"], "loss_pct": loss or None,
                             "need_qty": need_qty})
        row["norms"].append({"source": record["source"],
                             "source_status": record.get("source_status"),
                             "source_verified": record.get("source_verified", False)})
        if row["price"] is None and record.get("price") is not None:
            row["price"] = record["price"]

    for wcode, work in by_work.items():
        if wcode and wcode not in normed_works:
            add(CHECK_NO_NORM, LEVEL_WARNING,
                f"{work['code']} ({work.get('name') or 'вид работ'}): нормы расхода нет — "
                f"потребность в материалах не считается, а не берётся «на глаз»",
                work_code=work["code"])

    confirmed_money = 0.0
    reference_money = 0.0
    for row in rows.values():
        row["need_qty"] = round(row["need_qty"], 4)
        row["need_confirmed_qty"] = round(row["need_confirmed_qty"], 4)
        row["reference_qty"] = round(row["reference_qty"], 4)
        if row["price"] is None:
            row["need_rub"] = None
            row["need_confirmed_rub"] = None
            row["need_reference_rub"] = None
            add(CHECK_NO_PRICE, LEVEL_WARNING,
                f"{row['material_code'] or row['material_name']}: нет цены — потребность в деньгах "
                f"не считается (не ноль рублей)", material_code=row["material_code"])
            continue
        row["need_rub"] = round(row["need_qty"] * row["price"], 2)
        row["need_confirmed_rub"] = round(row["need_confirmed_qty"] * row["price"], 2)
        row["need_reference_rub"] = round(row["reference_qty"] * row["price"], 2)
        confirmed_money += row["need_confirmed_rub"]
        reference_money += row["need_reference_rub"]

    return {
        "rows": sorted(rows.values(), key=lambda r: -(r["need_qty"] or 0)),
        "checks": checks,
        "norms": {"total": len(norms or []), "applied": len([p for p in prepared if p["status"] == "ok"]),
                  "no_source": len(no_source), "unverified": len(unverified)},
        "totals": {"need_confirmed_rub": round(confirmed_money, 2),
                   "need_reference_rub": round(reference_money, 2)},
        "confirmed_by": ({"by": confirmed_by, "role": role} if confirmed_by and role and
                         confirm_norms else None),
    }


# ─────────────────────────── списание против нормы ───────────────────────────
def check_writeoff(need: dict, writeoffs: list[dict]) -> dict:
    """Списание по актам против потребности по норме. Перерасход без причины — ошибка."""
    checks: list[dict] = list(need.get("checks") or [])
    rows = {(_key({"code": r["material_code"], "name": r["material_name"]}), r["unit"]): r
            for r in need.get("rows") or []}

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    totals: dict[tuple[str, str], dict] = {}
    for item in writeoffs or []:
        qty = item.get("qty")
        if qty is None:
            add(CHECK_BAD_NUMBER, LEVEL_ERROR,
                f"списание {item.get('document') or item.get('file')}: количество "
                f"«{item.get('qty')}» не число — строка не учитывается",
                material_code=item.get("code") or None)
            continue
        key = (_key(item), item.get("unit") or "")
        slot = totals.setdefault(key, {"qty": 0.0, "documents": [], "reasons": [],
                                       "name": item.get("name"), "code": item.get("code")})
        slot["qty"] += qty
        if item.get("document"):
            slot["documents"].append(item["document"])
        if item.get("reason"):
            slot["reasons"].append(item["reason"])

    result_rows: list[dict] = []
    for key, row in rows.items():
        written = totals.pop(key, None)
        row = dict(row)
        row["written_qty"] = round(written["qty"], 4) if written else 0.0
        row["writeoff_reasons"] = (written or {}).get("reasons") or []
        row["writeoff_documents"] = (written or {}).get("documents") or []
        row["deviation_qty"] = round(row["written_qty"] - row["need_qty"], 4)
        price = row.get("price")

        if row["need_qty"] == 0:
            row["status"] = "нет потребности"
        elif row["written_qty"] == 0:
            row["status"] = "не списано"
            add(CHECK_NOT_WRITTEN_OFF, LEVEL_WARNING,
                f"{row['material_code'] or row['material_name']}: потребность "
                f"{_qty(row['need_qty'])} {row['unit']}, списания нет — работа закрыта без "
                f"списания материала?", material_code=row["material_code"])
        elif row["deviation_qty"] > TOL:
            money = round(row["deviation_qty"] * price, 2) if price is not None else None
            row["deviation_rub"] = money
            if row["writeoff_reasons"]:
                row["status"] = "перерасход с причиной"
                add(CHECK_OVER_WITH_REASON, LEVEL_WARNING,
                    f"{row['material_code'] or row['material_name']}: списано "
                    f"{_qty(row['written_qty'])} {row['unit']} против нормы "
                    f"{_qty(row['need_qty'])} — перерасход {_qty(row['deviation_qty'])}"
                    + (f" на {_rub(money)} ₽" if money is not None else "")
                    + f"; причина: {'; '.join(row['writeoff_reasons'])}",
                    material_code=row["material_code"], deviation_rub=money)
            else:
                row["status"] = "перерасход без причины"
                add(CHECK_OVER_NO_REASON, LEVEL_ERROR,
                    f"{row['material_code'] or row['material_name']}: списано "
                    f"{_qty(row['written_qty'])} {row['unit']} против нормы "
                    f"{_qty(row['need_qty'])} — перерасход {_qty(row['deviation_qty'])}"
                    + (f" на {_rub(money)} ₽" if money is not None else "")
                    + "; причина не указана — так списание не принимается",
                    material_code=row["material_code"], deviation_rub=money)
        elif row["deviation_qty"] < -TOL:
            row["status"] = "экономия"
            add(CHECK_NOT_WRITTEN_OFF, LEVEL_WARNING,
                f"{row['material_code'] or row['material_name']}: списано меньше нормы на "
                f"{_qty(abs(row['deviation_qty']))} {row['unit']} — проверить, не остались ли "
                f"материалы неописанными", material_code=row["material_code"])
        else:
            row["status"] = "по норме"
        result_rows.append(row)

    for key, written in totals.items():
        add(CHECK_WRITE_OFF_NO_NORM, LEVEL_ERROR,
            f"{written['code'] or written['name']}: списание {_qty(written['qty'])} без нормы "
            f"расхода — проверить нечем (норма не задана или не применена)",
            material_code=written["code"] or None)

    return {"rows": result_rows, "checks": checks}


# ─────────────────────────── лимит закупки ───────────────────────────
def check_limits(need: dict, purchases: list[dict]) -> dict:
    """Факт закупки против лимита (лимит = потребность по норме)."""
    checks: list[dict] = list(need.get("checks") or [])
    rows = {(_key({"code": r["material_code"], "name": r["material_name"]}), r["unit"]): r
            for r in need.get("rows") or []}

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    fact: dict[tuple[str, str], dict] = {}
    for item in purchases or []:
        qty = item.get("qty")
        price = item.get("price")
        if qty is None:
            add(CHECK_BAD_NUMBER, LEVEL_ERROR,
                f"закупка {item.get('document') or item.get('file')}: количество "
                f"«{item.get('qty')}» не число — строка не учитывается",
                material_code=item.get("code") or None)
            continue
        key = (_key(item), item.get("unit") or "")
        slot = fact.setdefault(key, {"qty": 0.0, "rub": 0.0, "has_price": False,
                                     "code": item.get("code"), "name": item.get("name")})
        slot["qty"] += qty
        if price is not None:
            slot["rub"] += qty * price
            slot["has_price"] = True

    result_rows: list[dict] = []
    for key, row in rows.items():
        bought = fact.pop(key, None)
        row = dict(row)
        row["purchased_qty"] = round(bought["qty"], 4) if bought else 0.0
        row["purchased_rub"] = round(bought["rub"], 2) if bought and bought["has_price"] else None
        row["limit_qty"] = row["need_qty"]
        row["limit_rub"] = row.get("need_rub")
        row["over_limit_qty"] = round(row["purchased_qty"] - row["limit_qty"], 4)
        if row["purchased_qty"] - row["limit_qty"] > TOL:
            over_money = (round(row["over_limit_qty"] * row["price"], 2)
                          if row.get("price") is not None else None)
            row["over_limit_rub"] = over_money
            add(CHECK_OVER_LIMIT, LEVEL_ERROR,
                f"{row['material_code'] or row['material_name']}: закуплено "
                f"{_qty(row['purchased_qty'])} {row['unit']} против лимита "
                f"{_qty(row['limit_qty'])} — превышение {_qty(row['over_limit_qty'])}"
                + (f" на {_rub(over_money)} ₽" if over_money is not None else "")
                + "; нужно решение человека с обоснованием", material_code=row["material_code"],
                over_limit_rub=over_money)
        if row.get("limit_rub") is None:
            add(CHECK_NO_PRICE, LEVEL_WARNING,
                f"{row['material_code'] or row['material_name']}: лимит в деньгах не посчитан — "
                f"нет цены (не ноль рублей)", material_code=row["material_code"])
        result_rows.append(row)

    for key, bought in fact.items():
        add(CHECK_PURCHASE_NO_NEED, LEVEL_WARNING,
            f"{bought['code'] or bought['name']}: закупка {_qty(bought['qty'])} без потребности "
            f"по норме — либо норма не задана, либо материал закуплен «в запас»",
            material_code=bought["code"] or None)

    return {"rows": result_rows, "checks": checks}


# ─────────────────────────── давальческие материалы ───────────────────────────
def check_daval(records: list[dict], purchases: list[dict] | None = None) -> dict:
    """Давальческие: получено / израсходовано / возвращено / остаток, без двойного счёта."""
    checks: list[dict] = []
    own = {_key({"code": i.get("code"), "name": i.get("name")}) for i in purchases or []}

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    rows: list[dict] = []
    for item in records or []:
        received = item.get("received")
        used = item.get("used")
        returned = item.get("returned")
        numbers = {"received": received, "used": used, "returned": returned}
        bad = [name for name, value in numbers.items()
               if value is None and name != "returned"]
        row = {"material_code": item.get("code") or None, "material_name": item.get("name"),
               "unit": item.get("unit"), "document": item.get("document"),
               **numbers}
        if bad:
            row["status"] = "нет данных"
            add(CHECK_BAD_NUMBER, LEVEL_ERROR,
                f"{row['material_code'] or row['material_name']}: не указано "
                f"{', '.join(bad)} — отчёт по давальческому материалу не строится",
                material_code=row["material_code"])
            rows.append(row)
            continue
        returned = returned or 0.0
        row["returned"] = returned
        row["remainder"] = round(received - used - returned, 4)
        if row["remainder"] < -TOL:
            row["status"] = "расход больше прихода"
            add(CHECK_DV_NEGATIVE, LEVEL_ERROR,
                f"{row['material_code'] or row['material_name']}: израсходовано и возвращено "
                f"больше, чем получено от заказчика на {_qty(abs(row['remainder']))} "
                f"{row['unit']} — либо часть работ сделана из своего материала, либо ошибка",
                material_code=row["material_code"])
        elif row["remainder"] > TOL and not returned:
            row["status"] = "остаток не возвращён"
            add(CHECK_DV_REMAINDER, LEVEL_WARNING,
                f"{row['material_code'] or row['material_name']}: остаток "
                f"{_qty(row['remainder'])} {row['unit']} не израсходован и не возвращён "
                f"заказчику — нужен акт возврата или списание", material_code=row["material_code"])
        else:
            row["status"] = "сходится" if row["remainder"] == 0 else "остаток возвращён"
        if _key({"code": row["material_code"], "name": row["material_name"]}) in own:
            row["status"] = "двойной счёт"
            add(CHECK_DV_DOUBLE, LEVEL_ERROR,
                f"{row['material_code'] or row['material_name']}: материал есть и в собственных "
                f"закупках, и в давальческих — двойной счёт (нельзя списать как свой)",
                material_code=row["material_code"])
        rows.append(row)

    return {"rows": rows, "checks": checks}


# ─────────────────────────── отчёт ───────────────────────────
def build_report(works: list[dict], norms: list[dict], *, writeoffs: list[dict] | None = None,
                 purchases: list[dict] | None = None, daval: list[dict] | None = None,
                 object_name: str = "", folder=None, confirmed_by: str = "",
                 role: str = "", confirm_norms: bool = False,
                 today: date | None = None) -> dict:
    """Собрать отчёт: потребность → списание → лимит → давальческие, с общим списком проверок."""
    today = today or date.today()
    need = build_need(works, norms, folder=folder, confirmed_by=confirmed_by, role=role,
                      confirm_norms=confirm_norms)
    writeoff = check_writeoff(need, writeoffs or [])
    limits = check_limits(need, purchases or [])
    daval_report = check_daval(daval or [], purchases)

    seen: set[tuple[str, str]] = set()
    checks: list[dict] = []
    for check in writeoff["checks"] + limits["checks"] + daval_report["checks"]:
        key = (check["code"], check["message"])
        if key in seen:
            continue
        seen.add(key)
        checks.append(check)

    errors = [c for c in checks if c["level"] == LEVEL_ERROR]
    warnings = [c for c in checks if c["level"] == LEVEL_WARNING]
    written_total = sum((r.get("written_qty") or 0) for r in writeoff["rows"])
    over = [r for r in writeoff["rows"] if (r.get("deviation_qty") or 0) > TOL]
    return {
        "schema": SCHEMA,
        "built_at": today.isoformat(),
        "object_name": object_name,
        "norms": need["norms"],
        "need": need["rows"],
        "writeoff": writeoff["rows"],
        "limits": limits["rows"],
        "daval": daval_report["rows"],
        "checks": checks,
        "totals": {
            "need_confirmed_rub": need["totals"]["need_confirmed_rub"],
            "need_reference_rub": need["totals"]["need_reference_rub"],
            "materials": len(need["rows"]),
            "written_qty": round(written_total, 3),
            "over_norms": len(over),
            "over_norms_rub": round(sum((r.get("deviation_rub") or 0) for r in over), 2),
            "over_limit": len([r for r in limits["rows"]
                               if (r.get("over_limit_qty") or 0) > TOL]),
            "over_limit_rub": round(sum((r.get("over_limit_rub") or 0)
                                        for r in limits["rows"]), 2),
            "daval_remainder": round(sum((r.get("remainder") or 0)
                                         for r in daval_report["rows"]), 3),
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "norm_confirmation": need["confirmed_by"],
        "disclaimer": ("Отчёт считает потребность по нормам и сверяет с ними списание и лимит. "
                       "Норма без источника не применяется; перерасход без причины — находка, "
                       "а не списание по факту. Решение о закупке и списании принимает человек."),
    }


def to_markdown(report: dict) -> str:
    totals = report.get("totals") or {}
    norms = report.get("norms") or {}
    lines = ["# Материалы: потребность, списание, лимит, давальческие", "",
             f"**Объект:** {report.get('object_name') or 'не указан'} · "
             f"**собрано:** {report.get('built_at')}", "",
             f"Материалов: {totals.get('materials')} · норм применено "
             f"{norms.get('applied')} из {norms.get('total')} (без источника "
             f"{norms.get('no_source')}, источник не подтверждён {norms.get('unverified')}) · "
             f"потребность подтверждённая: {_rub(totals.get('need_confirmed_rub'))} ₽ · "
             f"справочная: {_rub(totals.get('need_reference_rub'))} ₽", ""]
    if report.get("norm_confirmation"):
        who = report["norm_confirmation"]
        lines += [f"Нормы приняты для расчёта: {who['by']}, {who['role']}.", ""]

    errors = [c for c in report.get("checks") or [] if c["level"] == LEVEL_ERROR]
    warnings = [c for c in report.get("checks") or [] if c["level"] == LEVEL_WARNING]
    if errors:
        lines += ["## Ошибки: так принимать нельзя", ""]
        lines += [f"- **{c['code']}** — {c['message']}" for c in errors] + [""]
    if warnings:
        lines += ["## Требует решения", ""]
        lines += [f"- {c['code']} — {c['message']}" for c in warnings] + [""]

    if report.get("need"):
        lines += ["## Потребность по нормам", "",
                  "| Материал | Ед. | Потребность | Подтверждено | Справочно | Цена, ₽ | "
                  "Потребность, ₽ |", "|---|---|---|---|---|---|---|"]
        for row in report["need"]:
            lines.append(f"| {row['material_code'] or row['material_name']} — "
                         f"{row.get('material_name') or ''} | {row['unit']} | "
                         f"{_qty(row['need_qty'])} | {_qty(row['need_confirmed_qty'])} | "
                         f"{_qty(row['reference_qty'])} | {_rub(row.get('price'))} | "
                         f"{_rub(row.get('need_rub'))} |")
        lines.append("")

    if report.get("writeoff"):
        lines += ["## Списание против нормы", "",
                  "| Материал | Ед. | Норма | Списано | Отклонение | Статус |", "|---|---|---|---|---|---|"]
        for row in report["writeoff"]:
            lines.append(f"| {row['material_code'] or row['material_name']} | {row['unit']} | "
                         f"{_qty(row['need_qty'])} | {_qty(row['written_qty'])} | "
                         f"{_qty(row['deviation_qty'])} | {row['status']} |")
        lines.append("")

    if report.get("limits"):
        lines += ["## Лимит закупки", "",
                  "| Материал | Ед. | Лимит | Закуплено | Превышение | Лимит, ₽ | Факт, ₽ |",
                  "|---|---|---|---|---|---|---|"]
        for row in report["limits"]:
            lines.append(f"| {row['material_code'] or row['material_name']} | {row['unit']} | "
                         f"{_qty(row['limit_qty'])} | {_qty(row['purchased_qty'])} | "
                         f"{_qty(row['over_limit_qty'])} | {_rub(row.get('limit_rub'))} | "
                         f"{_rub(row.get('purchased_rub'))} |")
        lines.append("")

    if report.get("daval"):
        lines += ["## Давальческие материалы", "",
                  "| Материал | Ед. | Получено | Израсходовано | Возвращено | Остаток | Статус |",
                  "|---|---|---|---|---|---|---|"]
        for row in report["daval"]:
            lines.append(f"| {row['material_code'] or row['material_name']} | {row['unit']} | "
                         f"{_qty(row.get('received'))} | {_qty(row.get('used'))} | "
                         f"{_qty(row.get('returned'))} | {_qty(row.get('remainder'))} | "
                         f"{row['status']} |")
        lines.append("")

    lines += [f"> {report.get('disclaimer')}", ""]
    return "\n".join(lines)


def export_xlsx(report: dict, path: str | Path) -> Path:
    """Отчёт в XLSX: «Потребность», «Списание», «Лимит», «Давальческие», «Проверки»."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()

    def sheet(title: str, header: list[str], rows: list[list], widths: tuple, first: bool = False):
        ws = wb.active if first else wb.create_sheet(title=title)
        if first:
            ws.title = title
        ws.append(header)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in rows:
            ws.append(row)
        for column, width in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", widths):
            ws.column_dimensions[column].width = width
        return ws

    sheet("Потребность",
          ["Код", "Наименование", "Ед.", "Потребность", "Подтверждено базой", "Справочно",
           "Цена, ₽", "Потребность, ₽"],
          [[r["material_code"], r.get("material_name"), r["unit"], r["need_qty"],
            r["need_confirmed_qty"], r["reference_qty"], r.get("price"), r.get("need_rub")]
           for r in report.get("need") or []],
          (18, 40, 8, 14, 18, 12, 14, 18), first=True)
    sheet("Списание", ["Код", "Ед.", "Норма", "Списано", "Отклонение", "Отклонение, ₽",
                       "Причина", "Статус"],
          [[r["material_code"], r["unit"], r["need_qty"], r["written_qty"], r["deviation_qty"],
            r.get("deviation_rub"), "; ".join(r.get("writeoff_reasons") or []), r["status"]]
           for r in report.get("writeoff") or []], (18, 8, 14, 14, 14, 16, 30, 24))
    sheet("Лимит", ["Код", "Ед.", "Лимит", "Закуплено", "Превышение", "Лимит, ₽", "Факт, ₽"],
          [[r["material_code"], r["unit"], r["limit_qty"], r["purchased_qty"],
            r["over_limit_qty"], r.get("limit_rub"), r.get("purchased_rub")]
           for r in report.get("limits") or []], (18, 8, 14, 14, 14, 16, 16))
    sheet("Давальческие", ["Код", "Ед.", "Получено", "Израсходовано", "Возвращено", "Остаток",
                           "Статус"],
          [[r["material_code"], r["unit"], r.get("received"), r.get("used"),
            r.get("returned"), r.get("remainder"), r["status"]]
           for r in report.get("daval") or []], (18, 8, 14, 16, 14, 12, 26))
    sheet("Проверки", ["Уровень", "Код", "Сообщение"],
          [[c["level"], c["code"], c["message"]] for c in report.get("checks") or []],
          (10, 26, 120))
    wb.save(path)
    return path


def write(report: dict, out_dir: str | Path) -> dict:
    """Сохранить отчёт: JSON, Markdown, XLSX."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "consumption.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    (out / "consumption.md").write_text(to_markdown(report), encoding="utf-8")
    export_xlsx(report, out / "consumption.xlsx")
    return {"json": str(out / "consumption.json"), "markdown": str(out / "consumption.md"),
            "xlsx": str(out / "consumption.xlsx")}
