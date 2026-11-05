# -*- coding: utf-8 -*-
"""Сводка проверки: что проверено, что осталось и где нужно решение человека.

Зачем отдельный слой
--------------------
У нас много источников: сверка КС-2 (`summary.json`), выпуск со сканов
(`batch_summary.json`), деньги (`impact`), портфель актов, проверка чертежа по шаблону.
Каждый из них отвечает на свой вопрос, и инженер, глядя на пять файлов, всё равно не видит
главного: **можно подписывать или нет и что именно от меня нужно**.

`review` собирает их в один ответ с тремя разделами:

* **checked** — что реально проверено и с каким итогом (ok / warning / fail / skipped);
* **open_items** — что осталось незакрытым (не проверено, не оценено, отложено);
* **decisions** — где нужен человек: вопрос, почему он возник, что будет, если не решить,
  и какое действие предлагается.

Правила, которые держим:
  * отсутствующий артефакт — это `skipped`, а не «всё хорошо»;
  * «не оценено» не превращается в 0 ₽ и в нём же указывается, где смотреть;
  * вердикт `blocked` ставится только по блокирующим причинам, а не по любым предупреждениям.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

STATUS_OK, STATUS_WARNING, STATUS_FAIL, STATUS_SKIPPED = "ok", "warning", "fail", "skipped"
PRIORITY_BLOCKING, PRIORITY_IMPORTANT, PRIORITY_OPTIONAL = "blocking", "important", "optional"

VERDICT_BLOCKED, VERDICT_REVIEW, VERDICT_READY, VERDICT_NO_DATA = \
    "blocked", "needs_review", "ready", "no_data"


def load_json(path) -> dict | None:
    """Прочитать артефакт, если он есть. Отсутствие файла — не ошибка, а «не проверяли»."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────── отдельные проверки ──────────────────────────
def _check_ks2(summary: dict | None, source: str) -> tuple[dict, list, list]:
    """Сверка КС-2 с журналом и сметой."""
    if summary is None:
        return ({"id": "ks2", "name": "Сверка КС-2 с журналом и сметой", "status": STATUS_SKIPPED,
                 "source": source, "detail": "нет out/summary.json — сверка не выполнялась"}, [], [])

    discrepancies = summary.get("discrepancies") or []
    by_sev = summary.get("by_severity") or {}
    critical = int(by_sev.get("critical") or
                   sum(1 for d in discrepancies if d.get("severity") == "critical"))
    major = int(by_sev.get("major") or
                sum(1 for d in discrepancies if d.get("severity") == "major"))
    status = STATUS_FAIL if critical else (STATUS_WARNING if major else STATUS_OK)
    check = {
        "id": "ks2",
        "name": "Сверка КС-2 с журналом и сметой",
        "status": status,
        "source": source,
        "counts": {"lines": len(summary.get("lines") or []),
                   "discrepancies": len(discrepancies),
                   "critical": critical, "major": major},
        "money": {"claim_rub": _num(summary.get("amount_act")),
                  "blocked_rub": _num(summary.get("amount_blocked")),
                  "safe_rub": _num(summary.get("amount_act_safe"))},
        "detail": f"позиций {len(summary.get('lines') or [])}, расхождений {len(discrepancies)}"
                  f" (критичных {critical})",
    }
    decisions = []
    for d in discrepancies:
        if d.get("severity") != "critical":
            continue
        decisions.append({
            "priority": PRIORITY_BLOCKING,
            "question": f"{d.get('type')} по позиции {d.get('code') or '—'}: {d.get('name') or ''}".strip(),
            "why": d.get("message") or f"критичное расхождение {d.get('type')}",
            "action": d.get("suggested_action") or "подтвердить объём документами или снять строку из акта",
            "source": f"{source} · расхождение {d.get('type')}",
        })
    return check, decisions, []


def _check_release(batch: dict | None, source: str) -> tuple[dict, list, list]:
    """Выпуск исполнительной документации со сканов: листы, пометки, конфликты."""
    if batch is None:
        return ({"id": "release", "name": "Выпуск со сканов (redline)", "status": STATUS_SKIPPED,
                 "source": source, "detail": "нет сводки выпуска — сканы не обрабатывались"}, [], [])

    totals = batch.get("totals") or {}
    failed = int(totals.get("failed") or 0)
    conflicts = int(totals.get("conflicts") or 0)
    needs_human = int(totals.get("needs_human") or 0)
    unconfirmed_cal = int(totals.get("unconfirmed_calibration") or 0)
    status = STATUS_FAIL if failed else (STATUS_WARNING if (conflicts or needs_human) else STATUS_OK)
    check = {
        "id": "release",
        "name": "Выпуск со сканов (redline)",
        "status": status,
        "source": source,
        "counts": {"sheets": int(totals.get("sheets") or 0), "ok": int(totals.get("ok") or 0),
                   "failed": failed, "marks": int(totals.get("marks") or 0),
                   "conflicts": conflicts, "decisions": needs_human,
                   "unconfirmed_calibration": unconfirmed_cal},
        "detail": f"листов {totals.get('sheets') or 0} (ошибок {failed}), пометок "
                  f"{totals.get('marks') or 0}, конфликтов с ПТО {conflicts}",
    }
    decisions, open_items = [], []
    for sheet in batch.get("failed_sheets") or []:
        decisions.append({
            "priority": PRIORITY_BLOCKING,
            "question": f"Лист {sheet.get('sheet_id')} не распознан: {sheet.get('error')}",
            "why": "лист выпуска не проверен — подписывать комплект нельзя",
            "action": "переснять/пересканировать лист или внести пометки вручную",
            "source": f"{source} · лист {sheet.get('sheet_id')}",
        })
    # вопросы выпуска группируем по причине: 49 пометок без текста — это один вопрос, а не 49
    groups: dict[str, list] = {}
    for d in batch.get("decisions") or []:
        key = (d.get("reason") or "пометку нельзя применить автоматически").strip()
        groups.setdefault(key, []).append(d)
    for reason, items in groups.items():
        sheets = sorted({str(i.get("sheet_id")) for i in items if i.get("sheet_id")})
        actions = sorted({str(i.get("action")) for i in items if i.get("action")})
        head = items[0].get("text") or "пометки, требующие решения"
        question = head if len(items) == 1 else f"{head} — ещё {len(items) - 1} похожих ({len(items)} всего)"
        decisions.append({
            "priority": PRIORITY_IMPORTANT,
            "question": question,
            "why": reason,
            "action": actions[0] if actions else "решение инженера ПТО",
            "source": f"{source} · листы {', '.join(sheets[:4])}"
                      + (f" и ещё {len(sheets) - 4}" if len(sheets) > 4 else ""),
            "count": len(items),
        })
    if unconfirmed_cal:
        open_items.append({
            "kind": "calibration", "priority": PRIORITY_OPTIONAL,
            "detail": f"калибровка не подтверждена для {unconfirmed_cal} лист(ов): "
                      f"координаты могут быть смещены",
            "source": source,
            "action": "подтвердить калибровку или переснять лист",
        })
    cached = int(totals.get("sheets_from_cache") or 0)
    if cached:
        open_items.append({
            "kind": "cache", "priority": PRIORITY_OPTIONAL,
            "detail": f"{cached} лист(ов) взяты из кеша: распознавание не перезапускалось",
            "source": source, "action": "при сомнении прогнать заново с --no-resume",
        })
    return check, decisions, open_items


def _check_money(impact: dict | None, summary: dict | None, source: str) -> tuple[dict, list, list]:
    """Деньги: сколько под риском и сколько «не оценено»."""
    if impact is None:
        return ({"id": "money", "name": "Деньги под риском", "status": STATUS_SKIPPED,
                 "source": source, "detail": "нет расчёта денег — конфликты не переводились в рубли"},
                [], [])

    totals = impact.get("totals") or impact
    at_risk = _num(totals.get("at_risk_rub"))
    background = _num(totals.get("background_risk_rub"))
    total_risk = at_risk + background
    unpriced = impact.get("unpriced") or []
    unpriced_n = len(unpriced) or int(totals.get("items_unpriced") or 0)
    act_amount = _num(totals.get("act_amount_rub") or (summary or {}).get("amount_act"))
    share = round(100 * total_risk / act_amount, 2) if act_amount else None
    status = STATUS_WARNING if total_risk else STATUS_OK
    check = {
        "id": "money",
        "name": "Деньги под риском",
        "status": status,
        "source": source,
        "money": {"at_risk_rub": round(total_risk, 2),
                  "marks_risk_rub": round(at_risk, 2),
                  "background_risk_rub": round(background, 2),
                  "share_pct": share, "unpriced_positions": unpriced_n},
        "detail": f"под риском {total_risk:,.2f} ₽".replace(",", " ") +
                  (f" ({share} % акта)" if share is not None else "")
                  + (f", без цены {unpriced_n} поз." if unpriced_n else ""),
    }
    open_items = []
    if unpriced_n:
        open_items.append({
            "kind": "not_estimated", "priority": PRIORITY_IMPORTANT,
            "detail": f"{unpriced_n} позиц(ий) без цены: сумма не посчитана, «не оценено» ≠ 0 ₽",
            "source": source,
            "action": "дозаполнить цены в смете или подтвердить расценку",
        })
    return check, [], open_items


def _check_materials(report: dict | None, source: str) -> tuple[dict, list, list]:
    """Материалы: заявка ↔ приход ↔ КС-2. Деньги считаются по расхождениям с известной ценой."""
    if report is None:
        return ({"id": "materials", "name": "Материалы: заявка → приход → КС-2",
                 "status": STATUS_SKIPPED, "source": source,
                 "detail": "сверки материалов не было — накладные и акт не сверялись"},
                [], [])

    totals = report.get("totals") or {}
    frozen = _num(totals.get("frozen_rub"))
    at_risk = _num(totals.get("at_risk_rub"))
    claim = _num(totals.get("claim_risk_rub"))
    overpay = _num(totals.get("overpay_rub"))
    saving = _num(totals.get("saving_rub"))
    count = int(totals.get("discrepancies") or len(report.get("discrepancies") or []))
    unpriced = report.get("unpriced") or []
    act_unknown = (report.get("act_materials") or "").startswith("unknown")

    status = STATUS_WARNING if (frozen or at_risk or overpay or unpriced or act_unknown) else STATUS_OK
    check = {
        "id": "materials",
        "name": "Материалы: заявка → приход → КС-2",
        "status": status,
        "source": source,
        "money": {"at_risk_rub": round(at_risk, 2),          # claim_risk уже внутри суммы
                  "blocked_rub": round(frozen, 2),
                  "overpay_rub": round(overpay, 2),
                  "saving_rub": round(saving, 2),
                  "unpriced_positions": len(unpriced)},
        "detail": (f"расхождений {count}: заморожено {frozen:,.2f} ₽, "
                   f"под риском {at_risk:,.2f} ₽ (из них предъявлено без накладных "
                   f"{claim:,.2f} ₽)").replace(",", " ")
                  + (f", материалов в акте нет построчно — сверка «приход ↔ акт» не выполнялась"
                     if act_unknown else ""),
    }

    decisions, open_items = [], []
    for item in report.get("discrepancies") or []:
        if item.get("kind") not in ("ACT_WITHOUT_RECEIPT", "ACT_NOT_DELIVERED",
                                    "DELIVERED_NOT_ORDERED", "PRICE_UP"):
            continue
        money = item.get("money")
        decisions.append({
            "kind": "materials", "priority": PRIORITY_IMPORTANT,
            "question": f"{item['title']}: {item.get('code') or '—'}",
            "why": item.get("detail") or "",
            "if_not": (f"спор на {money:,.2f} ₽".replace(",", " ") if money
                       else "риск не оценён в деньгах"),
            "action": item.get("action") or "",
            "source": source,
        })
    if act_unknown:
        open_items.append({
            "kind": "materials_in_act_unknown", "priority": PRIORITY_IMPORTANT,
            "detail": "в акте нет построчных материалов: «материалов в акте нет» — не доказано",
            "source": source,
            "action": "получить КС-2 с построчными ресурсами или решить, что сверяем только приход",
        })
    if unpriced:
        open_items.append({
            "kind": "not_estimated", "priority": PRIORITY_OPTIONAL,
            "detail": f"{len(unpriced)} материал(ов) без цены: сумма не посчитана, это не 0 ₽",
            "source": source, "action": "дозаполнить цену в накладной или решении",
        })
    return check, decisions, open_items


def _check_portfolio(portfolio: dict | None, source: str) -> tuple[dict, list, list]:
    """Портфель актов: покрытие проверкой."""
    if portfolio is None:
        return ({"id": "portfolio", "name": "Портфель актов и покрытие проверкой",
                 "status": STATUS_SKIPPED, "source": source,
                 "detail": "нет реестра актов — покрытие не считалось"}, [], [])

    acts = portfolio.get("acts") or []
    unchecked = [a for a in acts if str(a.get("status") or "").lower() not in
                 ("проверен", "checked", "проверено")]
    totals = portfolio.get("totals") or {}
    coverage = totals.get("coverage_pct")
    status = STATUS_FAIL if unchecked and len(unchecked) == len(acts) else (
        STATUS_WARNING if unchecked else STATUS_OK)
    check = {
        "id": "portfolio",
        "name": "Портфель актов и покрытие проверкой",
        "status": status,
        "source": source,
        "counts": {"acts": len(acts), "checked": len(acts) - len(unchecked),
                   "unchecked": len(unchecked)},
        "money": {"checked_rub": _num(totals.get("checked_rub") or totals.get("amount_rub")),
                  "at_risk_rub": _num(totals.get("at_risk_rub"))},
        "detail": f"актов {len(acts)}, проверено {len(acts) - len(unchecked)}"
                  + (f", покрытие {coverage} %" if coverage is not None else ""),
    }
    decisions = [{
        "priority": PRIORITY_IMPORTANT,
        "question": f"Акт {a.get('act_id')} не проверен выпуском",
        "why": "акт подписан/предъявлен без сверки: риск пропустить завышение",
        "action": "прогнать ретро-выпуск по акту или принять риск письменно",
        "source": f"{source} · {a.get('act_id')}",
    } for a in unchecked]
    return check, decisions, []


def _check_cad(plan: dict | None, source: str) -> tuple[dict, list, list]:
    """Проверка чертежа по корпоративному шаблону и план правок."""
    if plan is None:
        return ({"id": "cad", "name": "Чертёж: шаблон и план правок", "status": STATUS_SKIPPED,
                 "source": source, "detail": "нет плана операций — чертёж не проверялся"}, [], [])

    validation = plan.get("validation") or {}
    blocking = validation.get("blocking")
    if blocking is None:
        blocking = sum(1 for i in validation.get("issues") or []
                       if i.get("severity") == "blocking")
    ops = (plan.get("summary") or {}).get("operations")
    if ops is None:
        ops = len(plan.get("operations") or [])
    status = STATUS_FAIL if int(blocking or 0) else (STATUS_OK if ops is not None else STATUS_SKIPPED)
    check = {
        "id": "cad",
        "name": "Чертёж: шаблон и план правок",
        "status": status,
        "source": source,
        "counts": {"operations": ops, "blocking": int(blocking or 0)},
        "detail": f"операций к применению {ops}, блокирующих нарушений шаблона {blocking or 0}",
    }
    decisions = []
    for issue in (validation.get("issues") or []):
        if issue.get("severity") != "blocking":
            continue
        decisions.append({
            "priority": PRIORITY_BLOCKING,
            "question": f"Блокирующее нарушение шаблона: {issue.get('message') or issue.get('type')}",
            "why": "чертёж не пройдёт нормоконтроль в текущем виде",
            "action": issue.get("suggested_action") or "исправить оформление и перепроверить",
            "source": f"{source} · {issue.get('type')}",
        })
    return check, decisions, []


# ─────────────────────────── сборка сводки ───────────────────────────────
def build_review(*, summary: dict | None = None, batch: dict | None = None,
                 acts: dict | None = None, impact: dict | None = None,
                 plan: dict | None = None, materials: dict | None = None,
                 sources: dict | None = None,
                 object_name: str | None = None) -> dict:
    """Собрать сводку проверки из готовых артефактов (всё необязательно)."""
    sources = sources or {}
    checks, decisions, open_items = [], [], []

    for builder, key, src in ((_check_ks2, "ks2", summary),
                              (_check_release, "batch", batch),
                              (_check_money, "impact", impact),
                              (_check_materials, "materials", materials),
                              (_check_portfolio, "acts", acts),
                              (_check_cad, "plan", plan)):
        if builder is _check_money:
            check, dec, items = builder(impact, summary, sources.get(key, "out/cad/impact"))
        elif builder is _check_cad:
            check, dec, items = builder(plan, sources.get(key, "out/cad/plan.json"))
        elif builder is _check_materials:
            check, dec, items = builder(materials, sources.get(key, "out/materials/materials.json"))
        else:
            check, dec, items = builder(src, sources.get(key, ""))
        checks.append(check)
        decisions.extend(dec)
        open_items.extend(items)

    # позиции без цены — из сводки ПТО (если расчёт денег не делался)
    if summary and not any(c["id"] == "money" and c["status"] != STATUS_SKIPPED for c in checks):
        unpriced = [l for l in summary.get("lines") or [] if not l.get("price")]
        if unpriced:
            open_items.append({
                "kind": "not_estimated", "priority": PRIORITY_IMPORTANT,
                "detail": f"{len(unpriced)} позиц(ий) сметы без цены: деньги по ним не считаются",
                "source": sources.get("ks2", ""), "action": "дозаполнить цены сметы",
            })

    blocking = sum(1 for d in decisions if d["priority"] == PRIORITY_BLOCKING)
    important = sum(1 for d in decisions if d["priority"] == PRIORITY_IMPORTANT)
    order = {PRIORITY_BLOCKING: 0, PRIORITY_IMPORTANT: 1, PRIORITY_OPTIONAL: 2}
    decisions.sort(key=lambda d: order.get(d["priority"], 3))

    performed = [c for c in checks if c["status"] != STATUS_SKIPPED]
    failed = [c for c in performed if c["status"] == STATUS_FAIL]
    warned = [c for c in performed if c["status"] == STATUS_WARNING]

    if not performed:
        verdict, can_sign = VERDICT_NO_DATA, False
        headline = "Проверок нет: ни один артефакт не найден — проверять нечего"
    elif blocking or failed:
        verdict, can_sign = VERDICT_BLOCKED, False
        headline = (f"Подписывать нельзя: блокирующих вопросов {blocking}, "
                    f"провалено проверок {len(failed)} из {len(performed)}")
    elif important or warned or open_items or not all(
            c["status"] != STATUS_SKIPPED for c in checks):
        verdict, can_sign = VERDICT_REVIEW, True
        skipped = len([c for c in checks if c["status"] == STATUS_SKIPPED])
        headline = (f"Можно подписывать с оговорками: проверок {len(performed)}, "
                    f"вопросов к человеку {important}, предупреждений {len(open_items)}"
                    + (f", не выполнялось {skipped}" if skipped else ""))
    else:
        verdict, can_sign = VERDICT_READY, True
        headline = f"Проверено {len(performed)} контуров, замечаний нет"

    money = {
        "at_risk_rub": round(sum(_num((c.get("money") or {}).get("at_risk_rub")) for c in checks), 2),
        "blocked_rub": round(sum(_num((c.get("money") or {}).get("blocked_rub")) for c in checks), 2),
        "claim_rub": round(sum(_num((c.get("money") or {}).get("claim_rub")) for c in checks), 2),
        "not_estimated_positions": sum(int(((c.get("money") or {}).get("unpriced_positions")) or 0)
                                       for c in checks)
        + sum(1 for i in open_items if i["kind"] == "not_estimated"),
    }

    next_steps = []
    if blocking:
        next_steps.append(f"снять {blocking} блокирующих вопрос(ов) — они первыми в decisions")
    if open_items:
        next_steps.append(f"закрыть {len(open_items)} открытых пункт(ов) или принять риск письменно")
    if any(c["status"] == STATUS_SKIPPED for c in checks):
        missing = ", ".join(c["id"] for c in checks if c["status"] == STATUS_SKIPPED)
        next_steps.append(f"дособрать артефакты для пропущенных проверок: {missing}")
    if not next_steps:
        next_steps.append("замечаний нет: можно оформлять решение человека в общем порядке")

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "object": object_name or (summary or {}).get("object") or (acts or {}).get("object"),
        "verdict": {"status": verdict, "can_sign": can_sign, "headline": headline,
                    "blocking": blocking, "important": important},
        "checked": checks,
        "open_items": open_items,
        "decisions": decisions,
        "money": money,
        "next_steps": next_steps,
    }


def review_to_markdown(review: dict) -> str:
    """Сводка для человека: одним экраном, без чтения JSON."""
    v = review["verdict"]
    icon = {VERDICT_READY: "✅", VERDICT_REVIEW: "⚠️", VERDICT_BLOCKED: "⛔",
            VERDICT_NO_DATA: "❔"}
    lines = [f"# Сводка проверки: {review.get('object') or 'объект не указан'}", "",
             f"**{icon.get(v['status'], '')} {v['headline']}**", "",
             f"Подпись возможна: {'да' if v['can_sign'] else 'нет'} · блокирующих "
             f"{v['blocking']} · к решению человеком {v['important']}", ""]

    money = review.get("money") or {}
    lines += ["## Деньги", "",
              f"- к предъявлению: {_fmt(money.get('claim_rub'))} ₽",
              f"- заблокировано: {_fmt(money.get('blocked_rub'))} ₽",
              f"- под риском: {_fmt(money.get('at_risk_rub'))} ₽",
              f"- позиций без оценки: {money.get('not_estimated_positions') or 0} "
              f"(«не оценено» ≠ 0 ₽)", ""]

    lines += ["## Что проверено", "", "| Проверка | Итог | Источник | Детали |", "|---|---|---|---|"]
    status_ru = {STATUS_OK: "ок", STATUS_WARNING: "замечания", STATUS_FAIL: "провалено",
                 STATUS_SKIPPED: "не выполнялась"}
    for c in review.get("checked") or []:
        lines.append(f"| {c['name']} | {status_ru.get(c['status'], c['status'])} | "
                     f"`{Path(c['source']).name if c.get('source') else '—'}` | {c.get('detail', '')} |")

    decisions = review.get("decisions") or []
    if decisions:
        lines += ["", "## Нужно решение человека", "",
                  f"Всего {len(decisions)}: блокирующих "
                  f"{sum(1 for d in decisions if d['priority'] == PRIORITY_BLOCKING)}, "
                  f"важных {sum(1 for d in decisions if d['priority'] == PRIORITY_IMPORTANT)}"
                  f"{', остальных ' + str(sum(1 for d in decisions if d['priority'] == PRIORITY_OPTIONAL)) if any(d['priority'] == PRIORITY_OPTIONAL for d in decisions) else ''}",
                  ""]
        limit = 25
        for i, d in enumerate(decisions[:limit], start=1):
            lines.append(f"{i}. **[{d['priority']}]** {d['question']}")
            lines.append(f"   - почему: {d['why']}")
            lines.append(f"   - что сделать: {d['action']}")
            lines.append(f"   - источник: {d['source']}")
        if len(decisions) > limit:
            lines.append(f"\n…и ещё {len(decisions) - limit} — полностью в `review.json`.")
    else:
        lines += ["", "## Нужно решение человека", "", "Вопросов нет."]

    if review.get("open_items"):
        lines += ["", "## Осталось открытым", ""]
        for item in review["open_items"]:
            lines.append(f"- [{item['priority']}] {item['detail']} → {item['action']}")

    lines += ["", "## Что дальше", ""]
    for step in review.get("next_steps") or []:
        lines.append(f"- {step}")
    return "\n".join(lines) + "\n"


def _fmt(value) -> str:
    try:
        return f"{float(value or 0):,.2f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def write_review(review: dict, out_dir: str | Path) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "review.json"
    md_path = out / "review.md"
    json_path.write_text(json.dumps(review, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    md_path.write_text(review_to_markdown(review), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def collect(*, summary_path=None, batch_path=None, acts_path=None, impact_path=None,
            plan_path=None, materials_path=None, object_name=None) -> dict:
    """Прочитать артефакты с диска и собрать сводку (пути могут отсутствовать)."""
    summary = load_json(summary_path)
    batch = load_json(batch_path)
    acts = load_json(acts_path)
    impact = load_json(impact_path)
    plan = load_json(plan_path)
    materials = load_json(materials_path)
    sources = {"ks2": str(summary_path or ""), "batch": str(batch_path or ""),
               "acts": str(acts_path or ""), "impact": str(impact_path or ""),
               "plan": str(plan_path or ""), "materials": str(materials_path or "")}
    return build_review(summary=summary, batch=batch, acts=acts, impact=impact, plan=plan,
                        materials=materials, sources=sources, object_name=object_name)
