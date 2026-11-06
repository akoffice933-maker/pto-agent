# -*- coding: utf-8 -*-
"""Портфель комплектов: по каким объектам можно предъявлять и сколько денег это держит.

Комплект по одному объекту отвечает на вопрос «можно ли сдать здесь». Руководителю нужен
следующий вопрос: **«где мы стоим и сколько это стоит»** — по всем объектам сразу.

Модуль держит три вещи, которые обычно теряются в сводках:

1. **Деньги привязаны к акту, а не к объекту «в целом».** Сумма берётся из акта КС-2
   (`amount_rub`) с указанием файла-источника. Нет суммы — это «не оценена», а не ноль:
   ноль в сводке читался бы как «там всё чисто».
2. **Деньги не считаются дважды.** Акт с одинаковым номером в двух строках портфеля —
   находка `ACT_DOUBLE_COUNTED`, сумма берётся один раз. В разрезе по разделам акт, заявленный
   в двух разделах, показывается в каждом — и это сказано в отчёте прямо, чтобы разрез не
   складывали с итогом.
3. **Сумма под риском разложена по действию.** «Поставить подпись», «устранить ошибки»,
   «собрать данные», «обосновать исключение» — разные работы и разные сроки. Деньги,
   которые держит только отсутствие подписи, снимаются за один вечер, и это должно быть видно.

Комплект объекта **не пересчитывается другим кодом**: портфель вызывает тот же
`pack.build_pack`, поэтому вердикт по объекту в сводке совпадает с вердиктом в папке объекта.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .pack import (SECTION_DISABLED, SECTION_MISSING, build_pack, can_submit)

SCHEMA = "pto-agent/pack-portfolio@1"

LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

# состояние акта в портфеле
ACT_AT_RISK = "под риском непредъявления"
ACT_RELEASABLE = "можно предъявлять"
ACT_NOT_ESTIMATED = "не оценено"

# что нужно сделать, чтобы деньги перестали висеть
FIX_SIGN = "поставить подписи"
FIX_ERRORS = "устранить ошибки в разделах"
FIX_DATA = "собрать отсутствующие разделы"
FIX_REASON = "обосновать исключение разделов или вернуть их в комплект"
FIX_ERRORS_SIGN = "устранить ошибки и поставить подписи"
FIX_MIXED = "и данные, и ошибки, и подписи"
FIX_NOTHING = "—"

CHECK_PORTFOLIO_EMPTY = "PORTFOLIO_EMPTY"
CHECK_OBJECT_WITHOUT_PACK = "OBJECT_WITHOUT_PACK"
CHECK_ACT_WITHOUT_AMOUNT = "ACT_WITHOUT_AMOUNT"
CHECK_ACT_AMOUNT_INVALID = "ACT_AMOUNT_INVALID"
CHECK_ACT_DOUBLE_COUNTED = "ACT_DOUBLE_COUNTED"
CHECK_ACT_WITHOUT_SECTIONS = "ACT_WITHOUT_SECTIONS"
CHECK_ACT_SECTION_UNKNOWN = "ACT_SECTION_UNKNOWN"
CHECK_RISK_NOT_ESTIMATED = "RISK_NOT_ESTIMATED"
CHECK_PACK_MISMATCH = "PACK_MISMATCH"


def _rub(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _money(value: float | None) -> str:
    if value is None:
        return "не оценено"
    return f"{value:,.2f} ₽".replace(",", " ")


def _num(value) -> float | None:
    """Сумма из файла: только конечное число. Строки вида «4 168 000,00» тоже читаются."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number and number not in (float("inf"), float("-inf")) else None
    text = str(value).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number else None


def _read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _resolve(value, base: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def _load_acts(value, base: Path) -> tuple[list[dict], str | None]:
    """Акты объекта: список в конфиге или ссылка на реестр актов (`data/demo/acts/acts.json`)."""
    if isinstance(value, list):
        return value, None
    if not value:
        return [], None
    path = _resolve(value, base)
    try:
        data = _read_json(path)
    except (json.JSONDecodeError, OSError) as exc:
        return [], f"{path.name}: реестр актов не читается ({exc.__class__.__name__})"
    acts = data.get("acts") if isinstance(data, dict) else data
    if not isinstance(acts, list):
        return [], f"{path.name}: в реестре нет списка актов"
    for act in acts:
        act.setdefault("_source", str(path))
    return acts, None


def _act_amount(act_data: dict, source: str) -> tuple[float | None, str | None]:
    """Сумма акта и проблема, если её нет.

    Порядок: `amount_rub` → `amount` → `сумма`. Строка «4 168 000» читается, `nan` — нет.
    Нет суммы — «не оценена» с причиной, никогда не ноль.
    """
    raw = act_data.get("amount_rub", act_data.get("amount", act_data.get("сумма")))
    where = act_data.get("_source") or source or "конфиг портфеля"
    if raw is None:
        return None, f"у акта нет суммы (искали amount_rub в {Path(where).name})"
    number = _num(raw)
    if number is None:
        return None, f"сумма акта «{raw}» не число ({Path(where).name})"
    if number <= 0:
        return None, f"сумма акта {number} ≤ 0 — акт не может держать деньги ({Path(where).name})"
    return _rub(number), None


def _section_money(pack: dict, blocked_acts: list[dict]) -> list[dict]:
    """Разрез «раздел → какие акты он держит и на сколько».

    Считается только по явному основанию: акт называет разделы в `covered_by`. Раздел, который
    акты не называют, денег не получает — иначе сумма появлялась бы из ниоткуда.
    """
    rows: list[dict] = []
    for section in pack.get("sections") or []:
        if section["status"] not in (SECTION_MISSING, SECTION_DISABLED) and \
                not section["errors"] and section["signed"]:
            continue                                   # раздел не мешает — денег не держит
        named = [act for act in blocked_acts if section["kind"] in (act.get("covered_by") or [])]
        rows.append({
            "section": section["kind"], "title": section["title"], "status": section["status"],
            "signed": section["signed"], "errors": len(section["errors"]),
            "acts": [act["act_id"] for act in named],
            "rub": _rub(sum(act["at_risk_rub"] or 0 for act in named)),
            "basis": "акты заявили раздел в covered_by" if named
                     else "акты этот раздел не заявляли — деньги к нему не отнесены",
        })
    return rows


def _fix_label(pack: dict) -> str:
    """Что мешает деньгам: одно действие, если оно одно, иначе «и то, и другое»."""
    missing = pack["totals"]["missing"]
    errors = pack["totals"]["with_errors"]
    no_reason = [s for s in pack.get("sections") or []
                 if s["status"] == SECTION_DISABLED and not s.get("why")]
    unsigned = [s for s in pack.get("sections") or []
                if not s["signed"] and s["status"] not in (SECTION_MISSING, SECTION_DISABLED)]
    kinds = [bool(missing), bool(errors), bool(no_reason), bool(unsigned)]
    if sum(kinds) == 0:
        return FIX_NOTHING
    if sum(kinds) == 1:
        return (FIX_DATA if missing else FIX_ERRORS if errors else
                FIX_REASON if no_reason else FIX_SIGN)
    if kinds == [False, False, False, True]:
        return FIX_SIGN
    if kinds[3] and not any(kinds[:3]):
        return FIX_SIGN
    if kinds[1] and kinds[3] and not kinds[0] and not kinds[2]:
        return FIX_ERRORS_SIGN
    return FIX_MIXED


def _pack_config(entry: dict, base: Path) -> tuple[dict | None, str | None]:
    """Конфиг комплекта объекта: путь или готовый словарь."""
    value = entry.get("pack")
    if isinstance(value, dict):
        return value, None
    if not value:
        return None, "в портфеле у объекта нет конфига комплекта"
    path = _resolve(value, base)
    try:
        return _read_json(path), None
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"{path.name}: конфиг комплекта не читается ({exc.__class__.__name__})"


def build_portfolio(config: dict, base_dir: str | Path = ".", out_dir: str | Path = "out/pack_portfolio",
                    today: date | None = None) -> dict:
    base = Path(base_dir)
    out = Path(out_dir)
    today = today or date.today()
    checks: list[dict] = []

    def add(code: str, level: str, message: str, **extra) -> None:
        checks.append({"code": code, "level": level, "message": message, **extra})

    objects_cfg = config.get("objects") or []
    objects: list[dict] = []
    seen_acts: dict[str, str] = {}
    known_amounts: list[float] = []
    risk_amounts: list[float] = []
    released_amounts: list[float] = []
    fix_buckets: dict[str, list[float]] = {}
    unestimated: list[dict] = []

    for entry in objects_cfg:
        name = entry.get("object_name") or entry.get("name") or "объект без названия"
        number = entry.get("pack_number") or entry.get("number") or ""
        pack_cfg, problem = _pack_config(entry, base)
        pack = None
        if pack_cfg is not None:
            object_out = out / (str(number or name).replace(" ", "_").replace("/", "-")[:40])
            pack = build_pack(pack_cfg, base_dir=base, out_dir=object_out, today=today)
        else:
            add(CHECK_OBJECT_WITHOUT_PACK, LEVEL_ERROR,
                f"объект «{name}»: {problem} — вердикта по комплекту нет, деньги объекта "
                f"«не оценены», а не «0 ₽ риска»", object=name)

        acts, acts_problem = _load_acts(entry.get("acts"), base)
        if acts_problem:
            add(CHECK_ACT_WITHOUT_AMOUNT, LEVEL_WARNING,
                f"объект «{name}»: {acts_problem}", object=name)
        source = str(entry.get("acts")) if isinstance(entry.get("acts"), str) else "конфиг портфеля"

        allowed = bool(pack and pack["can_submit"]["allowed"])
        blocked_sections = []
        blocked_codes = []
        if pack:
            # блокирует только то, что действительно мешает сдаче: отсутствующий раздел,
            # раздел с ошибками, неподписанный и исключённый БЕЗ причины. Исключённый
            # с основанием раздел сдаче не мешает — иначе причина исключения ничего не значит.
            blocked_sections = [
                s["kind"] for s in pack["sections"]
                if s["status"] == SECTION_MISSING
                or (s["status"] == SECTION_DISABLED and not s.get("why"))
                or s["errors"]
                or (not s["signed"] and s["status"] not in (SECTION_MISSING, SECTION_DISABLED))]
            blocked_codes = sorted({c["code"] for c in pack["checks"] if c["level"] == LEVEL_ERROR})
        fix = _fix_label(pack) if pack else FIX_DATA

        act_rows: list[dict] = []
        for act_data in acts:
            act_id = str(act_data.get("act_id") or act_data.get("number") or "").strip()
            if not act_id:
                act_id = "акт без номера"
            amount, amount_problem = _act_amount(act_data, source)
            row = {
                "act_id": act_id, "object": name, "pack_number": number,
                "period": act_data.get("period") or act_data.get("month") or "",
                "amount_rub": amount, "amount_status": "известна" if amount is not None else "не оценена",
                "amount_source": act_data.get("_source") or source,
                "covered_by": list(act_data.get("covered_by") or []),
                "attributed_sections": [], "blocked_by_sections": [], "blocker_codes": [],
                "fix": FIX_NOTHING,
                "at_risk_rub": None, "status": ACT_NOT_ESTIMATED,
            }
            if amount is not None:
                known_amounts.append(amount)
            if amount_problem:
                add(CHECK_ACT_WITHOUT_AMOUNT, LEVEL_ERROR,
                    f"{act_id} ({name}): {amount_problem} — деньги «не оценены», а не 0 ₽",
                    act=act_id, object=name)
                unestimated.append({"act_id": act_id, "object": name, "why": amount_problem})
            duplicate = act_id in seen_acts
            if duplicate:
                add(CHECK_ACT_DOUBLE_COUNTED, LEVEL_ERROR,
                    f"{act_id} встречается дважды («{seen_acts[act_id]}» и «{name}») — "
                    f"сумма берётся один раз, иначе риск удваивается", act=act_id)
                row.update({"status": ACT_NOT_ESTIMATED, "double_counted": True,
                            "why": f"акт уже учтён по объекту «{seen_acts[act_id]}»"})
                if amount is not None:
                    unestimated.append({"act_id": act_id, "object": name,
                                        "why": "акт повторён в портфеле — в итог не входит"})
                act_rows.append(row)
                continue
            seen_acts[act_id] = name

            if pack is None:
                row["why"] = "комплекта по объекту нет — предъявлять нечего, сумма не оценена"
                if amount is not None:
                    unestimated.append({"act_id": act_id, "object": name,
                                        "why": "комплект объекта не собран"})
                    add(CHECK_RISK_NOT_ESTIMATED, LEVEL_WARNING,
                        f"{act_id}: комплекта нет, {_money(amount)} не отнесены ни к риску, "
                        f"ни к предъявленному", act=act_id, object=name)
            elif allowed:
                row.update({"status": ACT_RELEASABLE, "at_risk_rub": 0.0})
                if amount is not None:
                    released_amounts.append(amount)
            else:
                row.update({"status": ACT_AT_RISK, "at_risk_rub": amount,
                            "blocked_by_sections": blocked_sections,
                            "attributed_sections": [k for k in row["covered_by"]
                                                    if k in blocked_sections],
                            "blocker_codes": blocked_codes, "fix": fix,
                            "hindrance": ("заявленные разделы: " + ", ".join(
                                k for k in row["covered_by"] if k in blocked_sections)
                                if any(k in blocked_sections for k in row["covered_by"])
                                else f"акт не заявил разделы — блокеров у объекта "
                                     f"{len(blocked_sections)}")})
                if amount is not None:
                    risk_amounts.append(amount)
                    fix_buckets.setdefault(fix, []).append(amount)
            if not row["covered_by"] and row["status"] == ACT_AT_RISK:
                add(CHECK_ACT_WITHOUT_SECTIONS, LEVEL_WARNING,
                    f"{act_id}: не заявлено, какие разделы комплекта его держат (covered_by) — "
                    f"в разрезе по разделам акт не показан", act=act_id, object=name)
            act_rows.append(row)

        if pack:
            kinds = {s["kind"] for s in pack["sections"]}
            for act in act_rows:
                unknown = [k for k in act["covered_by"] if k not in kinds]
                if unknown:
                    add(CHECK_ACT_SECTION_UNKNOWN, LEVEL_WARNING,
                        f"{act['act_id']}: разделы {', '.join(unknown)} в комплекте не найдены — "
                        f"заявка на привязку игнорируется", act=act["act_id"], object=name)
                    act["covered_by"] = [k for k in act["covered_by"] if k in kinds]

        objects.append({
            "object_name": name, "pack_number": number,
            "pack_built": pack is not None,
            "can_submit": allowed,
            "reasons": (pack["can_submit"]["reasons"] if pack else
                        ["комплекта по объекту нет"]),
            "totals": (pack["totals"] if pack else None),
            "fix": fix,
            "acts": act_rows,
            "at_risk_rub": None if pack is None else
            _rub(sum(a["at_risk_rub"] or 0 for a in act_rows if a["status"] == ACT_AT_RISK)),
            "released_rub": _rub(sum(a["amount_rub"] or 0 for a in act_rows
                                     if a["status"] == ACT_RELEASABLE)),
            "sections": (_section_money(pack, [a for a in act_rows
                                               if a["status"] == ACT_AT_RISK]) if pack else []),
            "pack_files": (pack.get("files") or {}) if pack else {},
        })

    if not objects:
        add(CHECK_PORTFOLIO_EMPTY, LEVEL_ERROR,
            "в портфеле нет ни одного объекта — сводке не из чего считаться")

    # ─── итоги ───
    at_risk = _rub(sum(risk_amounts)) if risk_amounts else 0.0
    released = _rub(sum(released_amounts)) if released_amounts else 0.0
    known = _rub(sum(known_amounts)) if known_amounts else 0.0
    portfolio = {
        "schema": SCHEMA, "kind": "pack_portfolio",
        "title": "Портфель комплектов исполнительной документации",
        "name": config.get("portfolio") or config.get("name") or "Портфель объектов",
        "built_at": today.isoformat(),
        "objects": objects, "checks": checks,
        "totals": {
            "objects": len(objects),
            "allowed": len([o for o in objects if o["can_submit"]]),
            "blocked": len([o for o in objects if o["pack_built"] and not o["can_submit"]]),
            "without_pack": len([o for o in objects if not o["pack_built"]]),
            "acts": len([a for o in objects for a in o["acts"]]),
            "acts_at_risk": len([a for o in objects for a in o["acts"]
                                 if a["status"] == ACT_AT_RISK]),
            "acts_releasable": len([a for o in objects for a in o["acts"]
                                    if a["status"] == ACT_RELEASABLE]),
            "errors": len([c for c in checks if c["level"] == LEVEL_ERROR]),
            "warnings": len([c for c in checks if c["level"] == LEVEL_WARNING]),
        },
        "money": {
            "acts_rub": known,
            "at_risk_rub": at_risk,
            "released_rub": released,
            "unestimated_acts": len(unestimated),
            "unestimated": unestimated,
            "by_fix": [{"fix": label, "rub": _rub(sum(values)), "acts": len(values)}
                       for label, values in sorted(fix_buckets.items(), key=lambda x: -sum(x[1]))],
            "note": ("Суммы — из актов КС-2 (amount_rub) с указанием источника. Акт без суммы "
                     "попадает в «не оценено», а не в ноль. В разрезе по разделам акт считается "
                     "в каждом заявленном разделе, поэтому этот разрез не складывается с итогом "
                     "по портфелю."),
        },
        "disclaimer": ("Портфель показывает, по каким объектам комплект можно предъявлять и "
                       "сколько денег по актам это держит. Вердикт считает агент по разделам "
                       "комплекта; подписи ставит человек, решение о предъявлении принимает ИТР."),
    }
    portfolio["verdict"] = verdict(portfolio)
    files = write(portfolio, out)
    portfolio["files"] = files
    return portfolio


def verdict(portfolio: dict) -> dict:
    """Сводный ответ по портфелю: где стоим и сколько это стоит."""
    money = portfolio.get("money") or {}
    totals = portfolio.get("totals") or {}
    at_risk = money.get("at_risk_rub") or 0
    can_any = totals.get("allowed", 0) > 0
    reasons: list[str] = []
    if totals.get("blocked"):
        reasons.append(f"объектов с блокерами: {totals['blocked']} из {totals['objects']}")
    if totals.get("without_pack"):
        reasons.append(f"объектов без комплекта: {totals['without_pack']}")
    if money.get("unestimated_acts"):
        reasons.append(f"актов с неоценённой суммой или без привязки: {money['unestimated_acts']}")
    return {
        "any_releasable": can_any,
        "at_risk_rub": at_risk,
        "released_rub": money.get("released_rub") or 0,
        "reasons": reasons,
        "summary": (f"предъявлять можно по {totals.get('allowed', 0)} объектам из "
                    f"{totals.get('objects', 0)}; под риском непредъявления "
                    f"{_money(at_risk)}"),
    }


# ─────────────────────────── вывод ───────────────────────────
def to_markdown(portfolio: dict) -> str:
    totals, money = portfolio["totals"], portfolio["money"]
    lines = [f"# {portfolio['title']}", "",
             f"**{portfolio.get('name')}** · собран: {portfolio['built_at']}", "",
             f"Объектов: {totals['objects']} · можно предъявлять: {totals['allowed']} · "
             f"с блокерами: {totals['blocked']} · без комплекта: {totals['without_pack']}", "",
             f"Актов: {totals['acts']} · под риском непредъявления: {totals['acts_at_risk']} · "
             f"можно предъявлять: {totals['acts_releasable']}", "",
             f"**Под риском непредъявления: {_money(money['at_risk_rub'])}** · "
             f"предъявлено актов на {_money(money['released_rub'])} · "
             f"не оценено актов: {money['unestimated_acts']}", ""]

    if money["by_fix"]:
        lines += ["## Чем снимается риск", "",
                  "| Что сделать | Актов | Сумма |", "|---|---|---|"]
        lines += [f"| {row['fix']} | {row['acts']} | {_money(row['rub'])} |"
                  for row in money["by_fix"]]
        lines.append("")

    lines += ["## Объекты", "",
              "| Объект | Комплект | Вердикт | Разделов с ошибками | Подписано | "
              "Под риском | Что делать |",
              "|---|---|---|---|---|---|---|"]
    for obj in portfolio["objects"]:
        totals_obj = obj.get("totals") or {}
        close = "можно предъявлять" if obj["can_submit"] else "предъявлять нельзя"
        if not obj.get("pack_built"):
            close = "комплекта нет"
        lines.append(
            f"| {obj['object_name']} | {obj['pack_number'] or 'б/н'} | {close} | "
            f"{totals_obj.get('with_errors', '—')} | {totals_obj.get('signed', '—')} | "
            f"{_money(obj['at_risk_rub'])} | {obj['fix']} |")
    lines.append("")

    for obj in portfolio["objects"]:
        if not obj["acts"]:
            continue
        lines += [f"### {obj['object_name']}: акты", "",
                  "| Акт | Период | Сумма | Состояние | Что мешает |", "|---|---|---|---|---|"]
        for act in obj["acts"]:
            hindrance = (act.get("hindrance") or act.get("why")
                         or (", ".join(act["blocked_by_sections"]) if act["blocked_by_sections"]
                             else "—"))
            lines.append(f"| {act['act_id']} | {act['period'] or '—'} | {_money(act['amount_rub'])} | "
                         f"{act['status']} | {hindrance} |")
        lines.append("")

    sections = [(obj["object_name"], row) for obj in portfolio["objects"]
                for row in obj.get("sections") or [] if row["rub"]]
    if sections:
        lines += ["## Разделы, которые держат деньги", "",
                  "| Объект | Раздел | Состояние | Подписан | Актов | Сумма по заявленным актам |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {name} | {row['title']} | {row['status']} | "
                  f"{'да' if row['signed'] else 'нет'} | {len(row['acts'])} | "
                  f"{_money(row['rub'])} |" for name, row in sections]
        lines += ["", "> Разрез по разделам не складывается с итогом: акт, заявленный в двух "
                      "разделах, показан в каждом.", ""]

    blockers = [c for c in portfolio["checks"] if c["level"] == LEVEL_ERROR]
    if blockers:
        lines += ["## Блокеры сводки", ""] + \
                 [f"- **{c['code']}** — {c['message']}" for c in blockers] + [""]
    warnings = [c for c in portfolio["checks"] if c["level"] == LEVEL_WARNING]
    if warnings:
        lines += ["## Требует решения", ""] + \
                 [f"- {c['code']} — {c['message']}" for c in warnings] + [""]

    lines += ["## Вердикт", "", f"- {portfolio['verdict']['summary']}"]
    lines += [f"- {reason}" for reason in portfolio["verdict"]["reasons"]]
    lines += ["", f"> {portfolio['disclaimer']}", ""]
    return "\n".join(lines)


def export_xlsx(portfolio: dict, path: str | Path) -> Path:
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
    sheet("Объекты",
          ["Объект", "Комплект", "Можно предъявлять", "Разделов", "С ошибками", "Без данных",
           "Подписано", "Под риском, ₽", "Что делать", "Причины"],
          [30, 12, 16, 10, 12, 12, 11, 16, 34, 70],
          [[o["object_name"], o["pack_number"],
            "да" if o["can_submit"] else ("нет" if o["pack_built"] else "комплекта нет"),
            (o.get("totals") or {}).get("sections", ""), (o.get("totals") or {}).get("with_errors", ""),
            (o.get("totals") or {}).get("missing", ""), (o.get("totals") or {}).get("signed", ""),
            o["at_risk_rub"], o["fix"], "; ".join(o["reasons"])]
           for o in portfolio["objects"]])
    sheet("Акты",
          ["Акт", "Объект", "Период", "Сумма, ₽", "Сумма известна", "Состояние",
           "Заявленные разделы-блокеры", "Все блокеры объекта", "Что делать", "Источник суммы"],
          [16, 26, 16, 16, 14, 24, 40, 46, 30, 46],
          [[a["act_id"], a["object"], a["period"], a["amount_rub"], a["amount_status"],
            a["status"], ", ".join(a["attributed_sections"]),
            ", ".join(a["blocked_by_sections"]), a["fix"], a["amount_source"]]
           for o in portfolio["objects"] for a in o["acts"]])
    sheet("Разделы и деньги",
          ["Объект", "Раздел", "Состояние", "Подписан", "Находок", "Актов", "Сумма, ₽", "Основание"],
          [26, 36, 22, 10, 9, 8, 16, 60],
          [[o["object_name"], row["title"], row["status"], "да" if row["signed"] else "нет",
            row["errors"], len(row["acts"]), row["rub"],
            row["basis"]] for o in portfolio["objects"] for row in o.get("sections") or []])
    sheet("Блокеры", ["Уровень", "Код", "Что не так", "Объект", "Акт"],
          [10, 28, 90, 26, 16],
          [[("ошибка" if c["level"] == "error" else "внимание"), c["code"], c["message"],
            c.get("object", ""), c.get("act", "")] for c in portfolio["checks"]])
    wb.save(path)
    return path


def export_docx(portfolio: dict, path: str | Path) -> Path:
    from docx import Document
    from docx.shared import Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading("Портфель комплектов исполнительной документации", level=1)
    doc.add_paragraph(f"{portfolio.get('name')} · на {portfolio['built_at']}")
    totals, money = portfolio["totals"], portfolio["money"]
    run = doc.add_paragraph().add_run(
        f"Объектов: {totals['objects']}, можно предъявлять: {totals['allowed']}, "
        f"с блокерами: {totals['blocked']}, без комплекта: {totals['without_pack']}. "
        f"Под риском непредъявления: {_money(money['at_risk_rub'])} по "
        f"{totals['acts_at_risk']} актам, не оценено актов: {money['unestimated_acts']}.")
    run.bold = True

    doc.add_heading("Объекты", level=2)
    table = doc.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells,
                          ["Объект", "Комплект", "Вердикт", "Подписано", "Под риском", "Что делать"]):
        cell.text = text
    for obj in portfolio["objects"]:
        cells = table.add_row().cells
        cells[0].text = obj["object_name"]
        cells[1].text = obj["pack_number"] or "б/н"
        cells[2].text = ("можно предъявлять" if obj["can_submit"] else
                         "предъявлять нельзя" if obj["pack_built"] else "комплекта нет")
        cells[3].text = str((obj.get("totals") or {}).get("signed", "—"))
        cells[4].text = _money(obj["at_risk_rub"])
        cells[5].text = obj["fix"]

    if money["by_fix"]:
        doc.add_heading("Чем снимается риск", level=2)
        for row in money["by_fix"]:
            doc.add_paragraph(f"• {row['fix']}: {_money(row['rub'])} по {row['acts']} актам",
                              style="List Bullet")

    doc.add_paragraph()
    doc.add_paragraph(f"Вердикт: {portfolio['verdict']['summary']}")
    for reason in portfolio["verdict"]["reasons"]:
        doc.add_paragraph(f"• {reason}", style="List Bullet")
    doc.add_paragraph()
    doc.add_paragraph("Сводку подготовил: ______________________ / ______________________")
    doc.add_paragraph("Проверил (начальник ПТО): ______________________ / ______________________")
    doc.add_paragraph("Решение о предъявлении принял: ______________________ / ______________")
    doc.add_paragraph(portfolio["disclaimer"]).runs[0].font.size = Pt(9)
    doc.save(path)
    return path


def write(portfolio: dict, out_dir: str | Path) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tail = str(portfolio.get("name") or "портфель")
    for bad in (" ", "/", ",", "«", "»", '"', "\\"):
        tail = tail.replace(bad, "_" if bad == " " else "-")
    tail = tail.strip("_-")[:40] or "портфель"
    (out / f"portfolio_{tail}.json").write_text(
        json.dumps(portfolio, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / f"portfolio_{tail}.md").write_text(to_markdown(portfolio), encoding="utf-8")
    export_xlsx(portfolio, out / f"portfolio_{tail}.xlsx")
    export_docx(portfolio, out / f"portfolio_{tail}.docx")
    return {name: str(out / f"portfolio_{tail}.{ext}")
            for name, ext in (("json", "json"), ("markdown", "md"),
                              ("xlsx", "xlsx"), ("docx", "docx"))}
