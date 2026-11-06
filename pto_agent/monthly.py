# -*- coding: utf-8 -*-
"""Месячный план ПТО: от платежа назад к работам.

Цепочка кругов 13–15 идёт от документов к деньгам: комплект → портфель → платёж. Этот модуль
разворачивает её в обратную сторону и отвечает на вопрос, с которого начинается месяц:
**«какие работы дают платёж в этом месяце, какой документ нужен для каждой и чего у нас нет»**.

Требования не выдумываются: они берутся из матрицы `data/reference/work_type_requirements.json`
(вид работ → обязательные документы → протоколы → нормы). Позиции акта классифицируются тем же
классификатором, что выпуск ИД и акты по скрытым работам.

Правила честности:

1. **Позиция не опознана — «требование не заявлено»** (`WORK_TYPE_NOT_STATED`), а не «документы не
   нужны». Деньги такой позиции не уходят в «готово».
2. **Документ без ФИО и роли — не документ.** Он в списке «есть», но работа не готова
   (`DOC_UNSIGNED`): подписывает человек.
3. **Вид работ без требований в матрице** (демонтаж, вывоз мусора) — «требований не заявлено»
   (`NO_REQUIREMENTS`), это не «всё в порядке» и не ошибка.
4. **Нет цены у позиции — «не оценено»**, а не 0 ₽. Сумма позиций сверяется с суммой акта
   (`ACT_SUM_MISMATCH`): если не сходится, деньги работ не бьются с деньгами месяца.
5. **Срок — из графика.** Нет срока — «не заявлено», дни не считаются.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

SCHEMA = "pto-agent/monthly-plan@1"

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

WORK_READY = "готово к предъявлению"
WORK_NOT_READY = "не готово"
WORK_NO_REQUIREMENTS = "требований в матрице нет"

CHECK_ACTS_EMPTY = "MONTH_WITHOUT_ACTS"
CHECK_ACT_WITHOUT_POSITIONS = "ACT_WITHOUT_POSITIONS"
CHECK_WORK_TYPE_NOT_STATED = "WORK_TYPE_NOT_STATED"
CHECK_NO_REQUIREMENTS = "NO_REQUIREMENTS"
CHECK_DOC_MISSING = "DOC_MISSING"
CHECK_DOC_UNSIGNED = "DOC_UNSIGNED"
CHECK_DOC_UNKNOWN_KIND = "DOC_UNKNOWN_KIND"
CHECK_WORK_NOT_READY = "WORK_NOT_READY"
CHECK_ACT_SUM_MISMATCH = "ACT_SUM_MISMATCH"
CHECK_POSITION_WITHOUT_PRICE = "POSITION_WITHOUT_PRICE"
CHECK_POSITION_WITHOUT_NAME = "POSITION_WITHOUT_NAME"
CHECK_DEADLINE_NOT_STATED = "DEADLINE_NOT_STATED"
CHECK_DEADLINE_MISSED = "DEADLINE_MISSED"
CHECK_DEADLINE_SOON = "DEADLINE_SOON"
CHECK_MATRIX_MISSING = "REQUIREMENTS_MATRIX_MISSING"

MONTHS_RU = {1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
             7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"}


def _rub(value):
    return None if value is None else round(float(value), 2)


def _money(value) -> str:
    return "не оценено" if value is None else f"{value:,.2f} ₽".replace(",", " ")


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    text = str(value).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number else None


def _date(value):
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def month_label(month: str) -> str:
    try:
        year, number = str(month).split("-")
        return f"{MONTHS_RU[int(number)]} {year}"
    except (ValueError, KeyError):
        return str(month)


def _requirements(matrix_path: Path | None):
    """Матрица требований: тот же файл и тот же загрузчик, что в выпуске ИД."""
    from .asbuilt.requirements import load_matrix, required_kinds, work_type_requirements

    return load_matrix(matrix_path), required_kinds, work_type_requirements


def _classify(text: str, matrix: dict) -> list[dict]:
    from .asbuilt.requirements import classify_work

    return classify_work(text, matrix)


def build_plan(config: dict, base_dir: str | Path = ".", out_dir: str | Path = "out/monthly",
               today: date | None = None) -> dict:
    base = Path(base_dir)
    out = Path(out_dir)
    today = today or date.today()
    checks: list[dict] = []

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    matrix_path = config.get("requirements")
    matrix_path = (Path(matrix_path) if Path(matrix_path).is_absolute() else base / matrix_path) \
        if matrix_path else None
    try:
        matrix, required_kinds, work_type_requirements = _requirements(matrix_path)
    except (OSError, ValueError) as exc:
        add(CHECK_MATRIX_MISSING, LEVEL_ERROR,
            f"матрица требований не загружена ({exc.__class__.__name__}) — требования к документам "
            f"«не заявлены», план по документам не считается")
        matrix, required_kinds, work_type_requirements = None, None, None

    month = str(config.get("month") or "")
    acts_cfg = config.get("acts") or []
    if not acts_cfg:
        add(CHECK_ACTS_EMPTY, LEVEL_ERROR, "в плане нет ни одного акта месяца — считать нечего")

    acts: list[dict] = []
    actions: list[dict] = []
    for act_cfg in acts_cfg:
        act_id = str(act_cfg.get("act_id") or "акт без номера").strip()
        act_amount = _num(act_cfg.get("amount_rub"))
        deadline = _date(act_cfg.get("submission_deadline"))
        row: dict = {
            "act_id": act_id, "object": act_cfg.get("object") or config.get("object_name"),
            "amount_rub": _rub(act_amount),
            "submission_deadline": act_cfg.get("submission_deadline"),
            "days_to_deadline": (deadline - today).days if deadline else None,
            "works": [], "documents_provided": [],
        }
        if deadline is None:
            add(CHECK_DEADLINE_NOT_STATED, LEVEL_WARNING,
                f"{act_id}: срок предъявления не заявлен — «успеваем / не успеваем» не считается",
                act=act_id)

        documents = []
        for doc in act_cfg.get("documents") or []:
            documents.append({
                "kind": str(doc.get("kind") or "").strip(),
                "requirement": str(doc.get("requirement") or "").strip(),
                "number": doc.get("number"), "date": doc.get("date"),
                "signed_by": doc.get("signed_by"), "role": doc.get("role"),
                "signed": bool(str(doc.get("signed_by") or "").strip()
                               and str(doc.get("role") or "").strip()),
            })
            if matrix and documents[-1]["kind"] not in (matrix.get("document_kinds") or {}):
                add(CHECK_DOC_UNKNOWN_KIND, LEVEL_WARNING,
                    f"{act_id}: документ «{doc.get('number') or '—'}» вида «{documents[-1]['kind']}» — "
                    f"такого вида нет в матрице требований", act=act_id)
        row["documents_provided"] = documents

        positions = act_cfg.get("positions") or []
        if not positions:
            add(CHECK_ACT_WITHOUT_POSITIONS, LEVEL_ERROR,
                f"{act_id}: в акте нет позиций — работы месяца не определены", act=act_id)
        positions_sum = 0.0
        positions_known = True
        by_type: dict[str, dict] = {}
        for pos in positions:
            text = " ".join(str(pos.get(k) or "") for k in ("name", "work"))
            if not text.strip():
                add(CHECK_POSITION_WITHOUT_NAME, LEVEL_ERROR,
                    f"{act_id}: позиция без наименования (код {pos.get('code') or '—'}) — "
                    f"вид работ определить нельзя", act=act_id)
                continue
            amount = _num(pos.get("amount_rub"))
            if amount is None:
                qty, price = _num(pos.get("qty")), _num(pos.get("price"))
                amount = (qty * price) if qty is not None and price is not None else None
            if amount is None:
                positions_known = False
                add(CHECK_POSITION_WITHOUT_PRICE, LEVEL_WARNING,
                    f"{act_id}, «{text[:60]}»: нет цены или количества — сумма позиции "
                    f"«не оценена», а не 0 ₽", act=act_id)
            else:
                positions_sum += amount

            hits = _classify(text, matrix) if matrix else []
            if not hits:
                add(CHECK_WORK_TYPE_NOT_STATED, LEVEL_ERROR,
                    f"{act_id}, «{text[:60]}»: вид работ не опознан — требования к документам "
                    f"«не заявлены», деньги позиции {_money(amount)} держатся вне плана",
                    act=act_id)
                slot = by_type.setdefault("unknown", {
                    "work_type": None, "name": "Вид работ не опознан", "positions": [],
                    "amount_rub": 0.0, "amount_unknown": False, "status": WORK_NOT_READY,
                    "missing": [], "unsigned": [], "requirements_stated": False,
                    "required": [], "protocols": [], "norms": [],
                })
                slot["positions"].append({"name": text, "code": pos.get("code")})
                slot["amount_rub"] += amount or 0
                slot["amount_unknown"] = slot["amount_unknown"] or amount is None
                continue
            best = hits[0]
            slot = by_type.setdefault(best["work_type"], {
                "work_type": best["work_type"], "name": best["name"], "positions": [],
                "amount_rub": 0.0, "amount_unknown": False, "status": WORK_READY,
                "missing": [], "unsigned": [], "requirements_stated": True,
                "required": [], "protocols": [], "norms": [],
            })
            slot["positions"].append({"name": text, "code": pos.get("code"),
                                      "amount_rub": _rub(amount)})
            slot["amount_rub"] += amount or 0
            slot["amount_unknown"] = slot["amount_unknown"] or amount is None

        if act_amount is not None and positions and positions_known and \
                abs(positions_sum - act_amount) > 0.01:
            add(CHECK_ACT_SUM_MISMATCH, LEVEL_ERROR,
                f"{act_id}: сумма позиций {_money(positions_sum)} не сходится с суммой акта "
                f"{_money(act_amount)} — деньги работ не бьются с деньгами месяца", act=act_id)

        # ─── требования и наличие документов ───
        if matrix:
            items = required_kinds(list(by_type), matrix)
            descriptions = items["document_kinds"]
            for work_type, slot in by_type.items():
                if work_type == "unknown":
                    continue
                req = work_type_requirements(work_type, matrix)
                slot["required"] = list(req.get("required") or [])
                slot["protocols"] = list(req.get("protocols") or [])
                slot["norms"] = list(req.get("norms") or [])
                if not slot["required"] and not slot["protocols"]:
                    slot["status"] = WORK_NO_REQUIREMENTS
                    add(CHECK_NO_REQUIREMENTS, LEVEL_WARNING,
                        f"{act_id}, «{slot['name']}»: в матрице нет требований к документам — "
                        f"«требований не заявлено», это не «всё в порядке»", act=act_id)
                    continue
                for kind in slot["required"]:
                    provided = [d for d in documents if d["kind"] == kind]
                    if not provided:
                        slot["missing"].append({"kind": kind,
                                                "what": descriptions.get(kind, kind)})
                    elif not any(d["signed"] for d in provided):
                        slot["unsigned"].append({"kind": kind,
                                                 "what": descriptions.get(kind, kind),
                                                 "number": provided[0].get("number")})
                for protocol in slot["protocols"]:
                    provided = [d for d in documents
                                if d["kind"] == "протокол"
                                and d["requirement"].lower() == protocol.lower()]
                    if not provided:
                        slot["missing"].append({"kind": "протокол", "what": protocol})
                    elif not any(d["signed"] for d in provided):
                        slot["unsigned"].append({"kind": "протокол", "what": protocol,
                                                 "number": provided[0].get("number")})
                slot["status"] = (WORK_READY if not slot["missing"] and not slot["unsigned"]
                                  else WORK_NOT_READY)
                for item in slot["missing"]:
                    add(CHECK_DOC_MISSING, LEVEL_ERROR,
                        f"{act_id}, «{slot['name']}»: нет документа — {item['what']} "
                        f"(держит {_money(slot['amount_rub'])})", act=act_id,
                        work_type=work_type, kind=item["kind"])
                for item in slot["unsigned"]:
                    add(CHECK_DOC_UNSIGNED, LEVEL_ERROR,
                        f"{act_id}, «{slot['name']}»: документ {item.get('number') or ''} есть, но "
                        f"не подписан человеком с ФИО и ролью — {item['what']}", act=act_id,
                        work_type=work_type, kind=item["kind"])

        for slot in by_type.values():
            slot["amount_rub"] = _rub(slot["amount_rub"])
            row["works"].append(slot)
            if slot["status"] == WORK_NOT_READY:
                add(CHECK_WORK_NOT_READY, LEVEL_ERROR,
                    f"{act_id}, «{slot['name']}»: работа не готова к предъявлению — "
                    f"{len(slot['missing'])} документ(ов) нет, {len(slot['unsigned'])} не подписано",
                    act=act_id, work_type=slot["work_type"])
                for item in slot["missing"][:3]:
                    actions.append({"act": act_id, "work": slot["name"],
                                    "what": "получить", "document": item["what"],
                                    "rub": slot["amount_rub"]})
                for item in slot["unsigned"][:2]:
                    actions.append({"act": act_id, "work": slot["name"],
                                    "what": "подписать", "document": item["what"],
                                    "rub": slot["amount_rub"]})

        if deadline:
            not_ready = [w for w in row["works"] if w["status"] == WORK_NOT_READY]
            if not_ready and (deadline - today).days < 0:
                add(CHECK_DEADLINE_MISSED, LEVEL_ERROR,
                    f"{act_id}: срок предъявления {deadline.isoformat()} прошёл "
                    f"{(today - deadline).days} дн. назад, не готово работ "
                    f"{len(not_ready)} из {len(row['works'])}", act=act_id)
            elif not_ready and (deadline - today).days <= 7:
                add(CHECK_DEADLINE_SOON, LEVEL_WARNING,
                    f"{act_id}: до срока {(deadline - today).days} дн., не готово работ "
                    f"{len(not_ready)} из {len(row['works'])}", act=act_id)

        acts.append(row)

    actions.sort(key=lambda a: -(a["rub"] or 0))
    works = [w for act in acts for w in act["works"]]
    ready = [w for w in works if w["status"] == WORK_READY]
    waiting = [w for w in works if w["status"] == WORK_NOT_READY]
    plan = {
        "schema": SCHEMA, "kind": "monthly_plan",
        "title": "Месячный план ПТО: работы, документы, деньги",
        "name": config.get("name") or f"План на {month_label(month)}",
        "month": month, "month_label": month_label(month),
        "object_name": config.get("object_name"),
        "built_at": today.isoformat(),
        "matrix": ({"profile": matrix.get("profile"), "work_types": len(matrix.get("work_types") or [])}
                   if matrix else None),
        "acts": acts, "actions": actions, "checks": checks,
        "totals": {
            "acts": len(acts),
            "works": len(works),
            "ready": len(ready),
            "not_ready": len(waiting),
            "no_requirements": len([w for w in works
                                    if w["status"] == WORK_NO_REQUIREMENTS]),
            "docs_required": sum(len(w["required"]) + len(w["protocols"]) for w in works),
            "docs_missing": sum(len(w["missing"]) for w in works),
            "docs_unsigned": sum(len(w["unsigned"]) for w in works),
            "money_ready_rub": _rub(sum(w["amount_rub"] or 0 for w in ready)),
            "money_waiting_rub": _rub(sum(w["amount_rub"] or 0 for w in waiting)),
            "money_unknown_works": len([w for w in works if w["amount_unknown"]]),
            "unclassified_works": len([w for w in works if w["work_type"] is None]),
            "errors": len([c for c in checks if c["level"] == LEVEL_ERROR]),
            "warnings": len([c for c in checks if c["level"] == LEVEL_WARNING]),
        },
        "disclaimer": ("План показывает, какие работы месяца можно предъявить, а какие держит "
                       "документ. Требования берутся из матрицы (профиль заказчика), наличие — "
                       "из переданных документов; подписи ставит человек. Чего нет в файлах — "
                       "«не заявлено», а не «не нужно»."),
    }
    plan["verdict"] = verdict(plan)
    files = write(plan, out)
    plan["files"] = files
    return plan


def verdict(plan: dict) -> dict:
    totals = plan["totals"]
    reasons: list[str] = []
    if totals["not_ready"]:
        reasons.append(f"работ не готово: {totals['not_ready']} из {totals['works']}")
    if totals["docs_missing"]:
        reasons.append(f"документов не хватает: {totals['docs_missing']}")
    if totals["docs_unsigned"]:
        reasons.append(f"документов без подписи: {totals['docs_unsigned']}")
    if totals["unclassified_works"]:
        reasons.append(f"видов работ не опознано: {totals['unclassified_works']}")
    return {
        "ready_to_invoice": totals["not_ready"] == 0 and totals["works"] > 0,
        "money_ready_rub": totals["money_ready_rub"],
        "money_waiting_rub": totals["money_waiting_rub"],
        "summary": (f"готово к предъявлению {totals['ready']} из {totals['works']} видов работ: "
                    f"{_money(totals['money_ready_rub'])} можно предъявлять, "
                    f"{_money(totals['money_waiting_rub'])} держит документ"),
        "reasons": reasons,
    }


# ─────────────────────────── вывод ───────────────────────────
def to_markdown(plan: dict) -> str:
    totals = plan["totals"]
    lines = [f"# {plan['title']}", "",
             f"**{plan['name']}**" + (f" · {plan['object_name']}" if plan.get("object_name") else "")
             + f" · на {plan['built_at']}", ""]
    if plan.get("matrix"):
        lines += [f"Требования — матрица «{plan['matrix']['profile'].get('name')}» "
                  f"версии {plan['matrix']['profile'].get('version')}, "
                  f"{plan['matrix']['work_types']} видов работ.", ""]
    lines += [f"Актов: {totals['acts']} · работ: {totals['works']} · готово: {totals['ready']} · "
              f"не готово: {totals['not_ready']} · документов не хватает: "
              f"{totals['docs_missing']} · без подписи: {totals['docs_unsigned']}", "",
              f"**{plan['verdict']['summary']}**", ""]

    lines += ["## Что сделать в первую очередь", ""]
    if plan["actions"]:
        lines += ["| Действие | Документ | Работа | Акт | Держит |", "|---|---|---|---|---|"]
        for action in plan["actions"][:12]:
            lines.append(f"| {action['what']} | {action['document'][:64]} | {action['work'][:40]} | "
                         f"{action['act']} | {_money(action['rub'])} |")
        lines.append("")
    else:
        lines += ["- всё, что нужно по матрице, уже собрано и подписано", ""]

    for act in plan["acts"]:
        lines += [f"## {act['act_id']}"
                  + (f" · {act['object']}" if act.get("object") else ""), ""]
        deadline = act["submission_deadline"] or "срок не заявлен"
        if act.get("days_to_deadline") is not None:
            tail = (f"просрочен на {-act['days_to_deadline']} дн."
                    if act["days_to_deadline"] < 0 else f"осталось {act['days_to_deadline']} дн.")
            deadline += f" ({tail})"
        lines += [f"Сумма акта: {_money(act['amount_rub'])} · срок предъявления: {deadline}", "",
                  "| Вид работ | Позиций | Сумма | Состояние | Чего не хватает |",
                  "|---|---|---|---|---|"]
        for work in act["works"]:
            gaps = ([f"нет: {m['what']}" for m in work["missing"]]
                    + [f"не подписан: {u['what']}" for u in work["unsigned"]])
            lines.append(f"| {work['name']} | {len(work['positions'])} | "
                         f"{_money(work['amount_rub'])} | {work['status']} | "
                         f"{'; '.join(gaps)[:160] or '—'} |")
        lines.append("")

    blockers = [c for c in plan["checks"] if c["level"] == LEVEL_ERROR]
    if blockers:
        lines += ["## Блокеры", ""] + [f"- **{c['code']}** — {c['message']}" for c in blockers] + [""]
    warnings = [c for c in plan["checks"] if c["level"] == LEVEL_WARNING]
    if warnings:
        lines += ["## Требует решения", ""] + [f"- {c['code']} — {c['message']}"
                                               for c in warnings] + [""]
    lines += ["", f"> {plan['disclaimer']}", ""]
    return "\n".join(lines)


def export_xlsx(plan: dict, path: str | Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    fill = PatternFill("solid", fgColor="1F4E79")
    font = Font(color="FFFFFF", bold=True)

    def sheet(title, headers, widths, rows):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = fill
            cell.font = font
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        for row in rows:
            ws.append(row)
        for idx, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = width
        ws.freeze_panes = "A2"

    wb.remove(wb.active)
    sheet("Работы",
          ["Акт", "Объект", "Вид работ", "Позиций", "Сумма, ₽", "Состояние", "Нужные документы",
           "Чего нет", "Что не подписано"],
          [14, 24, 40, 8, 16, 22, 44, 40, 40],
          [[act["act_id"], act.get("object") or "", work["name"], len(work["positions"]),
            work["amount_rub"], work["status"],
            ", ".join(work["required"] + [p[:28] for p in work["protocols"]]),
            "; ".join(m["what"] for m in work["missing"]),
            "; ".join(u["what"] for u in work["unsigned"])]
           for act in plan["acts"] for work in act["works"]])
    sheet("Что сделать",
          ["Действие", "Документ", "Вид работ", "Акт", "Держит, ₽"],
          [14, 60, 36, 14, 16],
          [[a["what"], a["document"], a["work"], a["act"], a["rub"]] for a in plan["actions"]])
    sheet("Документы",
          ["Акт", "Вид документа", "Номер", "Дата", "ФИО", "Роль", "Подписан"],
          [14, 40, 16, 12, 22, 22, 10],
          [[act["act_id"], d["kind"] + (f" / {d['requirement']}" if d.get("requirement") else ""),
            d.get("number") or "", d.get("date") or "", d.get("signed_by") or "",
            d.get("role") or "", "да" if d["signed"] else "нет"]
           for act in plan["acts"] for d in act.get("documents_provided") or []])
    sheet("Блокеры", ["Уровень", "Код", "Что не так"],
          [10, 26, 110],
          [[("ошибка" if c["level"] == "error" else "внимание"), c["code"], c["message"]]
           for c in plan["checks"]])
    wb.save(path)
    return path


def export_docx(plan: dict, path: str | Path) -> Path:
    from docx import Document
    from docx.shared import Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading("Месячный план ПТО: работы, документы, деньги", level=1)
    doc.add_paragraph(f"{plan['name']}" + (f" · {plan['object_name']}"
                                           if plan.get("object_name") else "")
                      + f" · на {plan['built_at']}")
    totals = plan["totals"]
    run = doc.add_paragraph().add_run(
        f"Работ: {totals['works']}, готово: {totals['ready']}, не готово: {totals['not_ready']}. "
        f"Можно предъявлять на {_money(totals['money_ready_rub'])}, документ держит "
        f"{_money(totals['money_waiting_rub'])}.")
    run.bold = True

    if plan["actions"]:
        doc.add_heading("Что сделать", level=2)
        for action in plan["actions"][:10]:
            doc.add_paragraph(f"• {action['what']}: {action['document']} — "
                              f"{action['work']} ({_money(action['rub'])})", style="List Bullet")

    doc.add_heading("Работы месяца", level=2)
    table = doc.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells,
                          ["Акт", "Вид работ", "Сумма", "Состояние", "Чего не хватает"]):
        cell.text = text
    for act in plan["acts"]:
        for work in act["works"]:
            cells = table.add_row().cells
            cells[0].text = act["act_id"]
            cells[1].text = work["name"]
            cells[2].text = _money(work["amount_rub"])
            cells[3].text = work["status"]
            gaps = ([f"нет: {m['what']}" for m in work["missing"]]
                    + [f"не подписан: {u['what']}" for u in work["unsigned"]])
            cells[4].text = "; ".join(gaps)[:200] or "—"

    doc.add_paragraph()
    doc.add_paragraph(f"Вердикт: {plan['verdict']['summary']}")
    for reason in plan["verdict"]["reasons"]:
        doc.add_paragraph(f"• {reason}", style="List Bullet")
    doc.add_paragraph()
    doc.add_paragraph("План подготовил: ______________________ / ______________________")
    doc.add_paragraph("Проверил (начальник ПТО): ______________________ / ______________________")
    doc.add_paragraph("Документы получил и подписал: ______________________ / ________________")
    doc.add_paragraph(plan["disclaimer"]).runs[0].font.size = Pt(9)
    doc.save(path)
    return path


def write(plan: dict, out_dir: str | Path) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tail = str(plan.get("name") or "план")
    for bad in (" ", "/", ",", ":", "«", "»", '"', "\\"):
        tail = tail.replace(bad, "_" if bad == " " else "-")
    tail = tail.strip("_-")[:40] or "план"
    (out / f"monthly_{tail}.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    (out / f"monthly_{tail}.md").write_text(to_markdown(plan), encoding="utf-8")
    export_xlsx(plan, out / f"monthly_{tail}.xlsx")
    export_docx(plan, out / f"monthly_{tail}.docx")
    return {name: str(out / f"monthly_{tail}.{ext}")
            for name, ext in (("json", "json"), ("markdown", "md"),
                              ("xlsx", "xlsx"), ("docx", "docx"))}
