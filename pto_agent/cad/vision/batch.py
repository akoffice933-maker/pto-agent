# -*- coding: utf-8 -*-
"""Пакетная обработка выпуска: 30–50 листов со сканами за один сеанс.

Инженер присылает не один лист, а выпуск: десятки сканов, у каждого свой чертёж,
свои пометки и свои расхождения. Задача пакета — прогнать все листы одним запуском
и собрать сводку, по которой можно работать: сколько пометок, что ушло человеку,
какие конфликты с КС-2 и на каких позициях.

Что важно в реализации:

* **Ничего не теряется молча.** Ошибка на листе не роняет пакет: лист помечается
  ошибкой с текстом причины, остальные обрабатываются, а в сводке видно, каких
  листов не хватает.
* **Результат воспроизводим.** По каждому листу сохраняется JSON (пометки, план,
  предупреждения, хэш скана), сводка считается из этих файлов. Повторный запуск
  с `--resume` берёт готовые листы из кеша и не тратит GPU.
* **Всё, что нельзя решать автоматически, видно в сводке.** Пометки без текста,
  низкая уверенность, конфликты «согласовано»/объём — отдельные списки с указанием
  листа и номера пометки.
"""
import hashlib
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from ...impact import (assess_impact, impact_chart, impact_to_markdown,
                       write_impact_xlsx)
from ..drawing import Drawing
from ..ops import validate_operations
from ..template import load_template, validate_drawing
from .engine import analyze_scan
from .plan import plan_from_scan

DEFAULT_TEMPLATE = "config/cad_template_corp_A.yaml"
SHEET_KEYS = ("sheet_id", "title", "scan", "drawing", "anchors", "texts", "truth", "note")


# ── манифест ─────────────────────────────────────────────────────────────
def load_manifest(path: str | Path) -> list:
    """Манифест выпуска: JSON со списком листов или каталог со сканами.

    Формат JSON: ``{"sheets": [{sheet_id, scan, drawing, anchors?, texts?, truth?}, …]}``.
    Каталог: все PNG/JPG рядом; чертёж берётся из `default_drawing` (если задан).
    """
    path = Path(path)
    if path.is_dir():
        return manifest_from_dir(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    sheets = data.get("sheets") if isinstance(data, dict) else data
    if not sheets:
        raise ValueError(f"в манифесте {path} нет списка листов")
    base = path.parent
    prepared = []
    for i, s in enumerate(sheets, 1):
        if not isinstance(s, dict) or not s.get("scan"):
            raise ValueError(f"лист №{i} в манифесте без пути к скану")
        s = dict(s)
        s.setdefault("sheet_id", f"sheet-{i:02d}")
        for key in ("scan", "drawing", "anchors", "texts", "truth"):
            if s.get(key) and not Path(s[key]).is_absolute():
                for root in (Path("."), base):
                    candidate = root / s[key]
                    if candidate.exists():
                        s[key] = str(candidate)
                        break
        prepared.append(s)
    return prepared


def manifest_from_dir(directory: str | Path, *, default_drawing: str | None = None,
                      pattern: str = "*.png") -> list:
    """Собрать манифест из каталога со сканами (по имени файла как номеру листа)."""
    directory = Path(directory)
    sheets = []
    for i, scan in enumerate(sorted(directory.glob(pattern)), 1):
        if not default_drawing:
            raise ValueError("для каталога нужен чертёж по умолчанию (default_drawing)")
        sheets.append({"sheet_id": scan.stem, "title": scan.stem,
                       "scan": str(scan), "drawing": str(default_drawing)})
    if not sheets:
        raise ValueError(f"в {directory} нет сканов по маске {pattern}")
    return sheets


# ── один лист ────────────────────────────────────────────────────────────
def _sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def process_sheet(sheet: dict, *, engine: str = "vlm", template_path: str = DEFAULT_TEMPLATE,
                  summary: dict | None = None, out_dir: str | Path = "out/cad/batch",
                  log_path: str | Path | None = None, cache: bool = True,
                  plan_only: bool = True) -> dict:
    """Обработать один лист: скан → пометки → план → проверка. Чертёж не меняется."""
    out_dir = Path(out_dir)
    sheet_dir = out_dir / "sheets"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    sheet_id = sheet["sheet_id"]
    started = time.time()

    scan_path = Path(sheet["scan"])
    record = {
        "sheet_id": sheet_id, "title": sheet.get("title") or sheet_id,
        "scan": str(scan_path), "drawing": str(sheet["drawing"]),
        "engine_requested": engine, "note": sheet.get("note", ""),
        "scan_sha256": _sha256(scan_path)[:16] if scan_path.exists() else None,
    }

    cache_file = sheet_dir / f"{sheet_id}.json"
    if cache and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if (cached.get("scan_sha256") == record["scan_sha256"]
                    and cached.get("engine_requested") == engine):
                cached["from_cache"] = True
                return cached
        except json.JSONDecodeError:
            pass

    try:
        drawing = Drawing.open(sheet["drawing"])
        tpl = load_template(template_path)
        kw = {"engine": engine, "drawing": drawing}
        if log_path:
            kw["log_path"] = log_path
        if sheet.get("anchors"):
            anchors = json.loads(Path(sheet["anchors"]).read_text(encoding="utf-8"))
            if anchors.get("mode") == "photo" or len(anchors.get("anchors") or []) >= 4:
                kw["photo"] = anchors
            else:
                kw["calibration"] = anchors
        if sheet.get("texts"):
            kw["texts_path"] = sheet["texts"]
        if sheet.get("truth") and engine == "mock":
            kw["truth_path"] = sheet["truth"]

        run = analyze_scan(scan_path, **kw)
        plan = plan_from_scan(run, drawing, tpl, summary)
        issues = validate_operations(drawing.doc, plan["operations"], tpl)
        drawing_check = validate_drawing(drawing, tpl)

        record.update({
            "ok": True, "engine_used": run["engine_used"],
            "photo": run.get("photo"),
            "calibration": {k: v for k, v in run["calibration"].items()
                            if k in ("method", "confirmed", "mm_per_px", "rms", "scale_x")},
            "marks": run["stats"]["marks"],
            "actionable": run["stats"]["actionable"],
            "needs_human": run["stats"]["needs_human"],
            "rejected": run["stats"]["rejected"],
            "operations": plan["summary"]["operations"],
            "conflicts": plan["conflicts"],
            "decisions": plan["needs_human_decision"],
            "ops_blocking": len([i for i in issues if i["severity"] == "blocking"]),
            "drawing_blocking": drawing_check["blocking"],
            "warnings": run["warnings"],
            "mark_list": [{"mark_id": m["mark_id"], "type": m["type"],
                           "text": m["text"], "confidence": m["confidence"],
                           "requires_confirmation": m.get("requires_confirmation")}
                          for m in run["marks"]],
            "plan_operations": plan["operations"],
        })
    except Exception as exc:                                  # noqa: BLE001
        record.update({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "traceback": traceback.format_exc(limit=3),
                       "marks": 0, "actionable": 0, "needs_human": 0, "rejected": 0,
                       "operations": 0, "conflicts": [], "decisions": [],
                       "warnings": [], "mark_list": [], "plan_operations": []})

    record["duration_ms"] = int((time.time() - started) * 1000)
    cache_file.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str),
                          encoding="utf-8")
    return record


# ── пакет ────────────────────────────────────────────────────────────────
def run_batch(sheets: list, *, engine: str = "vlm", workers: int = 1,
              out_dir: str | Path = "out/cad/batch", template_path: str = DEFAULT_TEMPLATE,
              summary: dict | None = None, resume: bool = True,
              progress=None) -> dict:
    """Прогнать выпуск целиком и собрать сводку.

    `workers > 1` имеет смысл только для модельного движка: офлайн-CV рендерит
    эталон через matplotlib, а он не потокобезопасен — для него потоки отключаются.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "llm_calls.jsonl"
    started = time.time()
    notes = []
    if workers > 1 and engine != "vlm":
        notes.append(f"движок {engine}: потоки отключены (рендер matplotlib не потокобезопасен)")
        workers = 1

    def job(sheet):
        rec = process_sheet(sheet, engine=engine, template_path=template_path,
                            summary=summary, out_dir=out_dir, log_path=log_path,
                            cache=resume)
        if progress:
            progress(rec)
        return rec

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            records = list(pool.map(job, sheets))
    else:
        records = [job(s) for s in sheets]

    result = aggregate(records, engine=engine, started=started, notes=notes,
                       out_dir=out_dir, template_path=template_path)
    if summary:
        result["impact"] = assess_impact(result["conflicts"], summary,
                                         discrepancies=summary.get("discrepancies"))
    return result


def aggregate(records: list, *, engine: str, started: float,
              notes: list | None = None, out_dir: Path | None = None,
              template_path: str = DEFAULT_TEMPLATE) -> dict:
    """Сводка по выпуску: итоги, конфликты по типам, позиции, листы, предупреждения."""
    ok = [r for r in records if r.get("ok")]
    failed = [r for r in records if not r.get("ok")]

    conflicts_by_type, marks_by_type, conflicts_by_code = {}, {}, {}
    all_conflicts, all_decisions = [], []
    for r in ok:
        for m in r.get("mark_list") or []:
            marks_by_type[m["type"]] = marks_by_type.get(m["type"], 0) + 1
        for c in r.get("conflicts") or []:
            conflicts_by_type[c["type"]] = conflicts_by_type.get(c["type"], 0) + 1
            code = c.get("code") or "без шифра"
            conflicts_by_code[code] = conflicts_by_code.get(code, 0) + 1
            all_conflicts.append({"sheet_id": r.get("sheet_id"),
                                  "drawing": r.get("drawing"),
                                  "title": r.get("title"),
                                  "mark_id": c.get("mark_id"),
                                  "type": c["type"], "code": c.get("code"),
                                  "mark_text": c.get("mark_text", ""),
                                  "mark_value": c.get("mark_value"),
                                  "act_value": c.get("act_value"),
                                  "mark_qty": c.get("mark_qty"),
                                  "act_qty": c.get("act_qty"),
                                  "unit": c.get("unit"),
                                  "delta_qty": c.get("delta_qty"),
                                  "critical_types": c.get("critical_types"),
                                  "resolution": c.get("resolution", "")})
        for d in r.get("decisions") or []:
            all_decisions.append({"sheet_id": r.get("sheet_id"), "mark_id": d.get("mark_id"),
                                  "text": d.get("text", ""), "reason": d.get("reason", ""),
                                  "action": d.get("suggested_action", "")})

    totals = {
        "sheets": len(records),
        "ok": len(ok),
        "failed": len(failed),
        "marks": sum(r.get("marks") or 0 for r in ok),
        "actionable": sum(r.get("actionable") or 0 for r in ok),
        "needs_human": sum(r.get("needs_human") or 0 for r in ok),
        "rejected_by_checks": sum(r.get("rejected") or 0 for r in ok),
        "operations": sum(r.get("operations") or 0 for r in ok),
        "conflicts": len(all_conflicts),
        "decisions": len(all_decisions),
        "unconfirmed_calibration": len([r for r in ok
                                        if not (r.get("calibration") or {}).get("confirmed", True)]),
        "sheets_from_cache": len([r for r in ok if r.get("from_cache")]),
        "duration_s": round(time.time() - started, 1),
    }
    warnings = [w for r in ok for w in (r.get("warnings") or [])]
    unique_warnings = sorted(set(w.split(":")[0] for w in warnings))

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "engine": engine,
        "template": template_path,
        "totals": totals,
        "conflicts_by_type": conflicts_by_type,
        "conflicts_by_code": dict(sorted(conflicts_by_code.items(),
                                         key=lambda kv: -kv[1])),
        "marks_by_type": marks_by_type,
        "conflicts": all_conflicts,
        "decisions": all_decisions,
        "warnings_seen": unique_warnings,
        "notes": notes or [],
        "failed_sheets": [{"sheet_id": r.get("sheet_id"), "scan": r.get("scan"),
                           "error": r.get("error")} for r in failed],
        "sheets": [{
            "sheet_id": r.get("sheet_id"), "title": r.get("title"),
            "scan": r.get("scan"), "drawing": r.get("drawing"),
            "ok": r.get("ok", False), "error": r.get("error"),
            "engine": r.get("engine_used"),
            "photo": bool(r.get("photo")),
            "calibration_confirmed": (r.get("calibration") or {}).get("confirmed"),
            "marks": r.get("marks", 0), "actionable": r.get("actionable", 0),
            "needs_human": r.get("needs_human", 0), "rejected": r.get("rejected", 0),
            "operations": r.get("operations", 0),
            "conflicts": len(r.get("conflicts") or []),
            "decisions": len(r.get("decisions") or []),
            "ops_blocking": r.get("ops_blocking", 0),
            "drawing_blocking": r.get("drawing_blocking"),
            "duration_ms": r.get("duration_ms"),
            "from_cache": r.get("from_cache", False),
        } for r in records],
        "out_dir": str(out_dir) if out_dir else None,
    }


# ── отчёты ───────────────────────────────────────────────────────────────
def write_json(data: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def write_markdown(summary: dict, path: str | Path) -> Path:
    """Сводка для человека — то, что читает начальник ПТО, а не машина."""
    t = summary["totals"]
    lines = ["# Сводка по выпуску: распознавание пометок", "",
             f"**Сформировано:** {summary['generated_at']} · **движок:** {summary['engine']} · "
             f"**шаблон:** `{summary['template']}`", "",
             "## Итог", "",
             "| Показатель | Значение |", "|---|---|",
             f"| Листов в выпуске | {t['sheets']} |",
             f"| Обработано успешно | {t['ok']} |",
             f"| Ошибок | {t['failed']} |",
             f"| Пометок найдено | {t['marks']} |",
             f"| Можно обработать автоматически | {t['actionable']} |",
             f"| Вопросов человеку | {t['needs_human']} |",
             f"| Отклонено проверками (нет чернил и т.п.) | {t['rejected_by_checks']} |",
             f"| Типизированных операций | {t['operations']} |",
             f"| Конфликтов с КС-2/журналом | {t['conflicts']} |",
             f"| Калибровок без подтверждения | {t['unconfirmed_calibration']} |",
             f"| Время прогона | {t['duration_s']} с |", ""]

    if summary.get("impact"):
        lines += ["", impact_to_markdown(summary["impact"])]

    if summary["conflicts"]:
        lines += ["## Конфликты с данными ПТО", "",
                  "| Лист | Пометка | Тип | Позиция | Что делать |", "|---|---|---|---|---|"]
        for c in summary["conflicts"]:
            lines.append(f"| {c['sheet_id']} | {c['mark_id']} | {c['type']} | "
                         f"{c['code'] or '—'} | {c['resolution'][:120]} |")
        lines.append("")

    if summary["decisions"]:
        lines += ["## Вопросы человеку", "",
                  "| Лист | Пометка | Причина | Действие |", "|---|---|---|---|"]
        for d in summary["decisions"]:
            lines.append(f"| {d['sheet_id']} | {d['mark_id']} | {d['reason'][:90]} | "
                         f"{d['action'][:80]} |")
        lines.append("")

    lines += ["## Листы", "",
              "| Лист | Пометок | Авто | Человеку | Операций | Конфликтов | Ошибка |",
              "|---|---|---|---|---|---|---|"]
    for s in summary["sheets"]:
        lines.append(f"| {s['sheet_id']} | {s['marks']} | {s['actionable']} | "
                     f"{s['needs_human']} | {s['operations']} | {s['conflicts']} | "
                     f"{s['error'] or '—'} |")
    lines.append("")

    if summary["failed_sheets"]:
        lines += ["## Листы с ошибками", ""]
        for f in summary["failed_sheets"]:
            lines.append(f"- **{f['sheet_id']}** (`{f['scan']}`): {f['error']}")
        lines.append("")
    if summary["notes"]:
        lines += ["## Примечания прогона", ""] + [f"- {n}" for n in summary["notes"]] + [""]
    lines += ["## Что дальше", "",
              "1. Разобрать конфликты и вопросы человеку — они в отдельных таблицах выше.",
              "2. Сформировать план операций по выбранным листам: `cad redline-from-scan`.",
              "3. Применить изменения только после dry_run и подписи инженера ПТО.", ""]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_xlsx(summary: dict, path: str | Path) -> Path:
    """Сводка в XLSX: «Итог», «Листы», «Конфликты», «Вопросы», «Пометки по типам»."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    head_fill = PatternFill("solid", fgColor="1F3864")
    head_font = Font(color="FFFFFF", bold=True)

    def sheet(wb, title, header, rows, widths):
        ws = wb.create_sheet(title)
        ws.append(header)
        for c in range(1, len(header) + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill, cell.font = head_fill, head_font
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]
        from ...impact import _scalar
        for row in rows:
            ws.append([_scalar(v) for v in row])
        ws.freeze_panes = "A2"
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        return ws

    wb = Workbook()
    wb.remove(wb.active)
    t = summary["totals"]
    sheet(wb, "Итог", ["Показатель", "Значение"], [
        ["Сформировано", summary["generated_at"]],
        ["Движок распознавания", summary["engine"]],
        ["Корпоративный шаблон", summary["template"]],
        ["Листов в выпуске", t["sheets"]],
        ["Обработано успешно", t["ok"]],
        ["Ошибок", t["failed"]],
        ["Пометок найдено", t["marks"]],
        ["Можно обработать автоматически", t["actionable"]],
        ["Вопросов человеку", t["needs_human"]],
        ["Отклонено проверками", t["rejected_by_checks"]],
        ["Типизированных операций", t["operations"]],
        ["Конфликтов с КС-2/журналом", t["conflicts"]],
        ["Калибровок без подтверждения", t["unconfirmed_calibration"]],
        ["Листов взято из кеша", t["sheets_from_cache"]],
        ["Время прогона, с", t["duration_s"]],
    ], [42, 60])
    sheet(wb, "Листы", ["Лист", "Наименование", "Скан", "Пометок", "Авто", "Человеку",
                        "Операций", "Конфликтов", "Калибровка", "Ошибка", "Из кеша"],
          [[s["sheet_id"], s["title"], Path(s["scan"]).name, s["marks"], s["actionable"],
            s["needs_human"], s["operations"], s["conflicts"],
            "подтверждена" if s["calibration_confirmed"] else "НЕ подтверждена",
            s["error"] or "", "да" if s["from_cache"] else ""] for s in summary["sheets"]],
          [14, 30, 26, 10, 8, 10, 10, 11, 16, 40, 8])
    sheet(wb, "Конфликты", ["Лист", "Пометка", "Тип конфликта", "Позиция", "Текст пометки",
                            "Что делать"],
          [[c["sheet_id"], c["mark_id"], c["type"], c["code"] or "", c["mark_text"],
            c["resolution"]] for c in summary["conflicts"]],
          [14, 12, 22, 22, 40, 70])
    sheet(wb, "Вопросы человеку", ["Лист", "Пометка", "Причина", "Предлагаемое действие"],
          [[d["sheet_id"], d["mark_id"], d["reason"], d["action"]]
           for d in summary["decisions"]],
          [14, 12, 55, 55])
    sheet(wb, "Пометки по типам", ["Тип пометки", "Количество"],
          [[k, v] for k, v in sorted(summary["marks_by_type"].items(),
                                     key=lambda kv: -kv[1])], [24, 14])
    sheet(wb, "Конфликты по позициям", ["Шифр расценки", "Конфликтов"],
          [[k, v] for k, v in summary["conflicts_by_code"].items()], [28, 14])
    impact = summary.get("impact")
    if impact:
        t2 = impact["totals"]
        sheet(wb, "Деньги по выпуску", ["Показатель", "Значение"], [
            ["Сумма акта, ₽", t2["act_amount_rub"]],
            ["Под риском по пометкам выпуска, ₽", t2["at_risk_rub"]],
            ["  завышение против акта, ₽", t2["markup_rub"]],
            ["  недополучено, ₽", t2["underclaim_rub"]],
            ["Доля от акта, %", t2["share_of_act_pct"]],
            ["Фон ПТО (расхождения объекта), ₽", t2.get("background_risk_rub")],
            ["Позиций с суммой", t2["positions_priced"]],
            ["Строк без цены", t2["items_unpriced"]],
        ], [48, 32])
        sheet(wb, "Позиции в деньгах",
              ["Позиция", "Наименование", "Ед.", "Цена, ₽", "Сумма, ₽",
               "Источник", "Листов", "Виды риска"],
              [[p["code"] or "—", p["name"], p["unit"], p["price"], p["money_rub"],
                p.get("source"), len(p["sheets"]) or "—",
                "; ".join(dict.fromkeys(r["risk_title"] for r in p["risks"]))]
               for p in impact["positions"]],
              [20, 44, 8, 13, 15, 20, 9, 50])
        sheet(wb, "Расчёт по строкам",
              ["Источник", "Лист", "Пометка", "Позиция", "Риск", "Формула",
               "Сумма, ₽", "Пояснение"],
              [[i["source"], i.get("sheet_id") or "—", i.get("mark_id") or "—",
                i.get("code") or "—",
                __import__("pto_agent.impact", fromlist=["RISK_TITLES"])
                .RISK_TITLES.get(i["risk"], i["risk"]),
                i.get("formula") or "", i.get("money_rub"), i.get("note") or ""]
               for i in impact["items"]],
              [18, 16, 10, 20, 34, 40, 15, 55])
        sheet(wb, "Не оценено в деньгах",
              ["Источник", "Позиция", "Тип", "Почему нет суммы"],
              [[i["source"], i.get("code") or "—", i.get("conflict_type"),
                i.get("note") or ""] for i in impact["unpriced"]], [18, 22, 26, 70])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def write_reports(summary: dict, out_dir: str | Path) -> dict:
    """Записать JSON, Markdown и XLSX одной командой."""
    out_dir = Path(out_dir)
    paths = {
        "json": write_json(summary, out_dir / "batch_summary.json"),
        "markdown": write_markdown(summary, out_dir / "batch_summary.md"),
        "xlsx": write_xlsx(summary, out_dir / "batch_summary.xlsx"),
    }
    if summary.get("impact"):
        write_json(summary["impact"], out_dir / "impact.json")
        write_impact_xlsx(summary["impact"], out_dir / "impact.xlsx")
        chart = impact_chart(summary["impact"], out_dir / "impact_chart.png")
        if chart:
            paths["impact_chart"] = chart
        paths["impact_xlsx"] = out_dir / "impact.xlsx"
    return paths


__all__ = ["load_manifest", "manifest_from_dir", "process_sheet", "run_batch",
           "aggregate", "write_json", "write_markdown", "write_xlsx", "write_reports",
           "SHEET_KEYS"]
