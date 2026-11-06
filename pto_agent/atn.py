# -*- coding: utf-8 -*-
"""АТН: акт на вновь возникающие и дополнительные работы.

Дополнительные работы — самая конфликтная часть денег на объекте: подрядчик предъявляет их
в конце, заказчик спрашивает «а где это в проекте и кто разрешил». Поэтому акт собирается
только из позиций, у которых **есть основание** — лист рабочей документации, исполнительная
схема, письмо или задание заказчика, протокол. Позиция без основания в акт не попадает:
она остаётся в списке «нужно основание» со своей суммой, и это видно в итоге.

Правила, которые держит модуль:

* **нет основания — нет позиции в акте.** Основание = вид + ссылка (лист, ось, номер письма).
  «Устная договорённость» основанием не считается;
* **нет цены — нет суммы.** Цена берётся из сметы по коду или указывается явно; если её нет,
  позиция уходит в «нет цены» с количеством, но не с нулём рублей (ноль — это тоже цифра);
* **повтор не считается дважды.** Та же позиция (код + единица) внутри одного АТН и позиция,
  уже предъявленная в актах с тем же количеством, отклоняются с причиной;
* **превышение сметы — не ошибка, а суть АТН**, но оно помечается: по смете остаток, по АТН
  предъявляется больше — значит, основание обязательно;
* **подпись только человека.** Подтверждение — ФИО и роль, статус после этого «проверен
  инженером (не подписан заказчиком)»; сам агент ничего не согласовывает.

Основание для позиции обычно берётся из накопительной ведомости (`cumulative.py`): позиции вне
сметы и превышение по смете — это и есть кандидаты в АТН, но решает человек.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .fingerprint import check_signature, content_fingerprint
from .numeric import parse_number

SCHEMA = "pto-agent/atn@1"

STATUS_DRAFT = "черновик"
STATUS_CONFIRMED = "проверен инженером (не подписан заказчиком)"

BASIS_KINDS = ("лист РД", "исполнительная схема", "письмо", "задание заказчика",
               "протокол", "расчёт", "смета")

INCLUDED = "включено"
NEEDS_BASIS = "нужно основание"
NO_PRICE = "нет цены"
REJECTED = "отклонено"


def _rub(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _num(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")
    return text.rstrip("0").rstrip(",") if "," in text else text


def basis_text(basis: dict | None) -> str:
    """Основание одной строкой: «лист РД · КР1, лист 12, ось 3». Пусто — нет основания."""
    if not isinstance(basis, dict):
        return ""
    kind = str(basis.get("kind") or "").strip()
    ref = str(basis.get("ref") or "").strip()
    if not kind or not ref:
        return ""
    return f"{kind} · {ref}" if not basis.get("date") else f"{kind} · {ref} от {basis['date']}"


def build_atn(items: list[dict], *, number: str = "", object_name: str = "", contract: str = "",
              customer: str = "", contractor: str = "", doc_date: date | None = None,
              ledger: dict | None = None, estimate: dict[str, dict] | None = None,
              reason: str = "", today: date | None = None) -> dict:
    """Собрать черновик АТН из позиций. Ничего не додумывает: чего нет — в отдельный список."""
    today = today or date.today()
    doc_date = doc_date or today
    estimate = estimate or {}
    ledger = ledger or {}

    # контекст из ведомости: что уже предъявлено и какой остаток по смете
    claimed: dict[tuple[str, str], dict] = {}
    for row in ledger.get("rows") or []:
        claimed[(str(row.get("code") or ""), str(row.get("unit") or ""))] = {
            "qty_total": row.get("qty_total"), "estimate_qty": row.get("estimate_qty"),
            "remainder_qty": row.get("remainder_qty"), "acts": row.get("acts") or [],
        }

    included: list[dict] = []
    needs_basis: list[dict] = []
    no_price: list[dict] = []
    rejected: list[dict] = []
    seen: dict[tuple[str, str], str] = {}

    for index, item in enumerate(items or [], start=1):
        name = str(item.get("name") or "").strip()
        code = str(item.get("code") or "").strip()
        unit = str(item.get("unit") or "").strip()
        qty = parse_number(item.get("qty"))
        label = code or name or f"позиция №{index}"
        base = {"number": index, "code": code or None, "name": name, "unit": unit or None,
                "qty": qty, "note": str(item.get("note") or "")}

        if qty is None:
            rejected.append({**base, "why": f"количество «{item.get('qty')}» не число"})
            continue
        if qty <= 0:
            rejected.append({**base, "why": f"количество {_num(qty)} — не предъявляется"})
            continue

        book = estimate.get(code) if code else None
        price = parse_number(item.get("price"))
        price_source = "указана в позиции"
        if price is None and book:
            price = parse_number(book.get("price"))
            price_source = "смета по коду"
        if price is None and not book:
            price_source = ""
        if not name and book:
            name = str(book.get("name") or "")
            base["name"] = name
        if not unit and book:
            unit = str(book.get("unit") or "")
            base["unit"] = unit

        key = (code or name.strip().lower(), unit)
        if key in seen:
            rejected.append({**base, "why": f"такая позиция уже есть в этом акте "
                                            f"(строка {seen[key]}) — второй раз не считается"})
            continue
        seen[key] = str(index)

        basis = item.get("basis") or {}
        text = basis_text(basis)
        amount = round(qty * price, 2) if price is not None else None
        context = claimed.get((code, unit)) or {}
        remainder = context.get("remainder_qty")
        row = {**base, "price": price, "price_source": price_source, "amount_rub": amount,
               "basis": basis if text else None, "basis_text": text or None,
               "already_claimed_qty": context.get("qty_total"),
               "estimate_qty": context.get("estimate_qty"), "remainder_qty": remainder}

        if not text:
            row["status"] = NEEDS_BASIS
            row["why"] = ("основание не указано: нужен вид и ссылка (лист РД, схема, письмо) — "
                          "позиция в акт не входит")
            needs_basis.append(row)
            continue
        if price is None:
            row["status"] = NO_PRICE
            row["why"] = ("цены нет ни в позиции, ни в смете — сумма не считается "
                          "(не ноль рублей)")
            no_price.append(row)
            continue
        if price <= 0:
            row["status"] = NO_PRICE
            row["why"] = f"цена {_rub(price)} ₽ — не предъявляется"
            no_price.append(row)
            continue
        if remainder is not None and qty > remainder:
            row["excess_qty"] = round(qty - remainder, 3)
            row["note"] = (row["note"] + " · " if row["note"] else "") + \
                          (f"превышает остаток по смете на {_num(qty - remainder)} {unit} — "
                           f"это и есть дополнительная работа")
        row["status"] = INCLUDED
        included.append(row)

    to_claim = round(sum(r["amount_rub"] for r in included), 2)
    needs_basis_money = round(sum(r["amount_rub"] or 0 for r in needs_basis), 2)
    no_price_qty = len(no_price)

    draft = {
        "schema": SCHEMA,
        "kind": "atn",
        "title": "Акт на вновь возникающие и дополнительные работы",
        "number": number,
        "doc_date": doc_date.isoformat(),
        "built_at": today.isoformat(),
        "object_name": object_name or ledger.get("object") or "",
        "contract": contract or ledger.get("contract") or "",
        "customer": customer,
        "contractor": contractor,
        "reason": reason,
        "status": STATUS_DRAFT,
        "confirmed_by": None,
        "positions": included,
        "needs_basis": needs_basis,
        "no_price": no_price,
        "rejected": rejected,
        "totals": {
            "positions_total": len(items or []),
            "included": len(included),
            "needs_basis": len(needs_basis),
            "no_price": no_price_qty,
            "rejected": len(rejected),
            "to_claim_rub": to_claim,
            "needs_basis_rub": needs_basis_money,
            "excess_vs_estimate_rub": round(
                sum((r.get("excess_qty") or 0) * (r.get("price") or 0) for r in included), 2),
        },
        "disclaimer": ("Черновик акта на дополнительные работы: сумма к предъявлению считает "
                       "только позиции с основанием и ценой. Акт подписывают заказчик и "
                       "подрядчик, агент его не согласовывает."),
    }
    draft["check"] = check_signature(draft)
    return draft


def confirm(atn: dict, *, by: str, role: str, reason: str = "", audit_log=None) -> dict:
    """Отметить проверку АТН человеком. Без ФИО и роли подтверждения нет."""
    problems: list[str] = []
    if not str(by or "").strip():
        problems.append("анонимное подтверждение: не указано ФИО проверившего")
    if not str(role or "").strip():
        problems.append("не указана роль (должность) проверившего")
    if not (atn.get("positions") or []):
        problems.append("в акте нет ни одной позиции с основанием — подтверждать нечего")
    if problems:
        atn = {**atn, "confirm_problems": problems}
        return atn

    atn = {**atn}
    atn["status"] = STATUS_CONFIRMED
    atn["confirmed_by"] = {"by": str(by).strip(), "role": str(role).strip(),
                           "reason": str(reason or ""), "at": date.today().isoformat(),
                           "positions": len(atn.get("positions") or [])}
    atn["confirmed_by"]["content_fingerprint"] = content_fingerprint(atn)
    atn.pop("confirm_problems", None)
    if audit_log is not None:
        audit_log.append("agent:atn", "agent", "atn.confirmed", f"atn:{atn.get('number') or 'б/н'}",
                         {"by": atn["confirmed_by"]["by"], "role": atn["confirmed_by"]["role"],
                          "to_claim_rub": atn["totals"]["to_claim_rub"],
                          "positions": atn["totals"]["included"]})
    return atn


def to_markdown(atn: dict) -> str:
    totals = atn.get("totals") or {}
    lines = ["# Акт на вновь возникающие и дополнительные работы (черновик)", "",
             f"**Объект:** {atn.get('object_name') or 'не указан'} · "
             f"**договор:** {atn.get('contract') or 'не указан'} · "
             f"**акт №:** {atn.get('number') or 'б/н'} от {atn.get('doc_date')}", ""]
    if atn.get("customer") or atn.get("contractor"):
        lines += [f"Заказчик: {atn.get('customer') or '—'} · Подрядчик: "
                  f"{atn.get('contractor') or '—'}", ""]
    lines += [f"**К предъявлению:** {_rub(totals.get('to_claim_rub'))} ₽ по "
              f"{totals.get('included')} позициям · требует основания: "
              f"{totals.get('needs_basis')} поз. на {_rub(totals.get('needs_basis_rub'))} ₽ · "
              f"без цены: {totals.get('no_price')} · отклонено: {totals.get('rejected')}", ""]

    if atn.get("positions"):
        lines += ["## Позиции акта", "",
                  "| № | Наименование | Ед. | Кол-во | Цена, ₽ | Сумма, ₽ | Основание |",
                  "|---|---|---|---|---|---|---|"]
        for row in atn["positions"]:
            lines.append(f"| {row['number']} | {row.get('name') or row.get('code') or '—'} | "
                         f"{row.get('unit') or '—'} | {_num(row.get('qty'))} | "
                         f"{_rub(row.get('price'))} | {_rub(row.get('amount_rub'))} | "
                         f"{row.get('basis_text')} |")
        lines += ["", f"**Итого: {_rub(totals.get('to_claim_rub'))} ₽**", ""]
    else:
        lines += ["Позиций с основанием и ценой нет: акт пуст, и это честный результат, а не "
                  "ошибка расчёта.", ""]

    if atn.get("needs_basis"):
        lines += ["## Нужно основание (в акт не вошло)", ""]
        for row in atn["needs_basis"]:
            lines.append(f"- {row.get('code') or row.get('name') or '—'}: "
                         f"{_num(row.get('qty'))} {row.get('unit') or ''} на "
                         f"{_rub(row.get('amount_rub'))} ₽ — {row.get('why')}")
        lines.append("")
    if atn.get("no_price"):
        lines += ["## Без цены (сумма не считается)", ""]
        for row in atn["no_price"]:
            lines.append(f"- {row.get('code') or row.get('name') or '—'}: "
                         f"{_num(row.get('qty'))} {row.get('unit') or ''} — {row.get('why')}")
        lines.append("")
    if atn.get("rejected"):
        lines += ["## Отклонено при сборке", ""]
        for row in atn["rejected"]:
            lines.append(f"- {row.get('code') or row.get('name') or '—'}: {row.get('why')}")
        lines.append("")
    if atn.get("confirmed_by"):
        by = atn["confirmed_by"]
        lines += [f"Проверено: {by.get('by')}, {by.get('role')} ({by.get('at')}). "
                  f"Статус: {atn.get('status')}.", ""]
    lines += [f"> {atn.get('disclaimer')}", ""]
    return "\n".join(lines)


def export_docx(atn: dict, path: str | Path) -> Path:
    """АТН в DOCX: шапка, таблица позиций с основанием, итог, место для подписей."""
    from docx import Document
    from docx.shared import Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading("Акт на вновь возникающие и дополнительные работы", level=1)
    doc.add_paragraph(f"Объект: {atn.get('object_name') or '—'}　"
                      f"Договор: {atn.get('contract') or '—'}")
    doc.add_paragraph(f"Акт № {atn.get('number') or 'б/н'} от {atn.get('doc_date')}　"
                      f"Заказчик: {atn.get('customer') or '—'}　"
                      f"Подрядчик: {atn.get('contractor') or '—'}")
    if atn.get("reason"):
        doc.add_paragraph(f"Основание выпуска акта: {atn['reason']}")

    positions = atn.get("positions") or []
    table = doc.add_table(rows=1, cols=7)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells,
                          ["№", "Наименование работ", "Ед.", "Кол-во", "Цена, ₽", "Сумма, ₽",
                           "Основание"]):
        cell.text = text
    for row in positions:
        cells = table.add_row().cells
        cells[0].text = str(row["number"])
        cells[1].text = str(row.get("name") or row.get("code") or "")
        cells[2].text = str(row.get("unit") or "")
        cells[3].text = _num(row.get("qty"))
        cells[4].text = _rub(row.get("price"))
        cells[5].text = _rub(row.get("amount_rub"))
        cells[6].text = str(row.get("basis_text") or "")
    doc.add_paragraph()
    total = doc.add_paragraph()
    run = total.add_run(f"Итого к предъявлению: {_rub((atn.get('totals') or {}).get('to_claim_rub'))} ₽")
    run.bold = True
    run.font.size = Pt(11)

    if atn.get("needs_basis"):
        doc.add_paragraph("Не включено в акт — требуется основание:")
        for row in atn["needs_basis"]:
            doc.add_paragraph(f"• {row.get('code') or row.get('name') or '—'}: "
                              f"{_num(row.get('qty'))} {row.get('unit') or ''} на "
                              f"{_rub(row.get('amount_rub'))} ₽", style="List Bullet")
    if atn.get("no_price"):
        doc.add_paragraph("Не включено — нет цены (сумма не считается):")
        for row in atn["no_price"]:
            doc.add_paragraph(f"• {row.get('code') or row.get('name') or '—'}: "
                              f"{_num(row.get('qty'))} {row.get('unit') or ''}",
                              style="List Bullet")

    doc.add_paragraph()
    doc.add_paragraph("Составил (инженер ПТО): ______________________ / ______________________")
    doc.add_paragraph("Проверил: ______________________ / ______________________")
    doc.add_paragraph("Согласовано (заказчик): ______________________ / ______________________")
    doc.add_paragraph(f"Статус документа: {atn.get('status')}. "
                      f"{atn.get('disclaimer')}").runs[0].font.size = Pt(9)
    doc.save(path)
    return path


def write(atn: dict, out_dir: str | Path, *, stem: str | None = None) -> dict:
    """Сохранить АТН: JSON, Markdown, DOCX. Имя файла — по номеру акта."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tail = stem or (str(atn.get("number") or "").replace(" ", "_").replace("/", "-") or "atn")
    (out / f"atn_{tail}.json").write_text(json.dumps(atn, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    (out / f"atn_{tail}.md").write_text(to_markdown(atn), encoding="utf-8")
    export_docx(atn, out / f"atn_{tail}.docx")
    return {"json": str(out / f"atn_{tail}.json"), "markdown": str(out / f"atn_{tail}.md"),
            "docx": str(out / f"atn_{tail}.docx")}
