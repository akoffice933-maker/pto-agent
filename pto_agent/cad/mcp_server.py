# -*- coding: utf-8 -*-
"""MCP-сервер CAD-модуля: инструменты агента по протоколу Model Context Protocol.

Транспорты:
  * stdio (по умолчанию) — для MCP-клиентов (Claude Desktop, IDE, собственный агент);
  * http  — POST /mcp (JSON-RPC 2.0) + страница-инспектор на GET / (для проверки руками).

Каждый инструмент — обёртка над функциями пакета pto_agent.cad. Инструменты
не меняют чертёж без подтверждения человека: apply_operations в режиме commit
требует объект confirm {by, role, comment}.
"""
import argparse
import hmac
import json
import os
import re
import secrets
import sys
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..audit import AuditLog
from ..impact import assess_impact, impact_chart, write_impact_xlsx
from ..portfolio import (assess_portfolio, load_acts, write_portfolio_reports)
from .diff import diff_drawings
from .drawing import Drawing
from .external_takeoff import (align, load_config, local_positions_from_dxf,
                              local_positions_from_summary, run_external_takeoff)
from .ops import OPERATIONS, apply_operations, history_entry, validate_operations
from ..review import collect as collect_review
from ..review import review_to_markdown
from ..asbuilt import (aosr_to_markdown, build_aosr, build_registry, build_scheme,
                       check_completeness, matrix_summary, registry_to_markdown,
                       scheme_to_markdown, spec_from_estimate, spec_to_markdown,
                       validate_draft, work_type_requirements)
from ..asbuilt.acts_plan import plan_acts, plan_to_markdown, plan_to_specs
from ..asbuilt.forms import forms_catalog
from ..asbuilt.norms_check import norms_to_markdown, verify_norms
from ..asbuilt.spec_from_estimate import load_positions
from .redline import plan_from_discrepancies
from .render import render_png
from .takeoff import compare_takeoff, takeoff
from .template import load_template, validate_drawing
from .vision.batch import load_manifest, run_batch, write_reports
from .vision.engine import analyze_scan
from .vision.plan import plan_from_scan

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "pto-cad-agent", "version": "0.1.0"}
DEFAULT_TEMPLATE = "config/cad_template_corp_A.yaml"

# ─────────────────────────── инструменты ─────────────────────────────────
TOOLS = []


def tool(name, description, schema):
    def deco(fn):
        TOOLS.append({"name": name, "description": description, "inputSchema": schema, "fn": fn})
        return fn
    return deco


def _p(args, key, required=True, default=None):
    v = args.get(key, default)
    if v in (None, "") and required:
        raise ValueError(f"не передан обязательный параметр {key}")
    return v


def _load_doc_spec(path: Path) -> dict:
    """Спецификация ИД из JSON/YAML: одинаково для CLI и MCP."""
    raw = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(raw) or {}
    return json.loads(raw)


@tool("get_drawing_info",
      "Метаданные чертежа: единицы, слои, блоки, стили, штамп, объёмы на схеме. "
      "Всегда первый вызов перед любыми операциями.",
      {"type": "object", "properties": {
          "file_path": {"type": "string", "description": "путь к DXF"},
          "template_path": {"type": "string", "default": DEFAULT_TEMPLATE}},
       "required": ["file_path"]})
def t_get_drawing_info(args, ctx):
    d = Drawing.open(_p(args, "file_path"))
    inv = d.inventory()
    tpl = load_template(args.get("template_path") or DEFAULT_TEMPLATE)
    return {
        "drawing": inv,
        "titleblock": d.titleblock(tpl.titleblock_block),
        "takeoff_positions": takeoff(d),
        "template": {"name": tpl.name, "frame": tpl.frame_block,
                     "min_text_height_mm": tpl.min_text_height_mm},
        "note": "координаты и объёмы берутся из файла; модель их не придумывает",
    }


@tool("cad_validate_template",
      "Проверка чертежа на соответствие корпоративному шаблону (слои, блоки, шрифты, "
      "высота текста, штамп, рамка). Blocking-замечания запрещают выпуск схемы.",
      {"type": "object", "properties": {
          "file_path": {"type": "string"},
          "template_path": {"type": "string", "default": DEFAULT_TEMPLATE}},
       "required": ["file_path"]})
def t_validate_template(args, ctx):
    d = Drawing.open(_p(args, "file_path"))
    tpl = load_template(args.get("template_path") or DEFAULT_TEMPLATE)
    return validate_drawing(d, tpl)


@tool("analyze_redline",
      "Пометки к исполнительной схеме по найденным расхождениям ПТО: строит "
      "типизированные операции с привязкой к объектам чертежа. Пометки с уверенностью "
      "ниже 0.7 не превращаются в операции, а попадают в needs_human_decision.",
      {"type": "object", "properties": {
          "file_path": {"type": "string"},
          "discrepancies_path": {"type": "string",
                                 "description": "summary.json прогона ПТО"},
          "discrepancies": {"type": "array", "items": {"type": "object"},
                            "description": "либо список расхождений напрямую"},
          "template_path": {"type": "string", "default": DEFAULT_TEMPLATE}},
       "required": ["file_path"]})
def t_analyze_redline(args, ctx):
    d = Drawing.open(_p(args, "file_path"))
    tpl = load_template(args.get("template_path") or DEFAULT_TEMPLATE)
    if args.get("discrepancies") is not None:
        summary = {"discrepancies": args["discrepancies"]}
    else:
        path = Path(_p(args, "discrepancies_path"))
        summary = json.loads(path.read_text(encoding="utf-8"))
    plan = plan_from_discrepancies(summary, d, tpl)
    plan["validation"] = validate_drawing(d, tpl)
    return plan


@tool("scan_redline_image",
      "Распознать пометки (redline) на скане/фото листа: калибровка скана по габаритам "
      "DXF, локальная VL-модель по API (или офлайн-CV), проверка каждого bbox по чернилам. "
      "Возвращает пометки с координатами в модели чертежа и статистику; ничего не меняет.",
      {"type": "object", "properties": {
          "scan_path": {"type": "string", "description": "скан/фото листа (PNG/JPG)"},
          "file_path": {"type": "string", "description": "DXF для калибровки (обязателен)"},
          "engine": {"type": "string", "enum": ["auto", "vlm", "offline", "mock"],
                     "default": "vlm"},
          "texts_path": {"type": "string",
                         "description": "JSON с текстами, введёнными инженером"},
          "truth_path": {"type": "string", "description": "эталон для engine=mock"},
          "calibration": {"type": "object", "description": "готовая калибровка ед/px"},
          "provider": {"type": "string", "description": "id VL-модели из llm_providers.yaml"},
          "confirm_calibration": {"type": "boolean", "default": False}},
       "required": ["scan_path", "file_path"]})
def t_scan_redline_image(args, ctx):
    d = Drawing.open(_p(args, "file_path"))
    kw = {"engine": args.get("engine") or "vlm",
          "log_path": Path(ctx.get("base_dir", ".")) / "out/cad/llm_calls.jsonl"}
    for src, dst in (("texts_path", "texts_path"), ("truth_path", "truth_path"),
                     ("provider", "provider_id"), ("calibration", "calibration")):
        if args.get(src):
            kw[dst] = args[src]
    if args.get("confirm_calibration"):
        kw["confirm_calibration"] = True
    run = analyze_scan(_p(args, "scan_path"), drawing=d, **kw)
    run["next_step"] = ("plan_from_scan_redline → validate_operations → apply_operations "
                        "(commit только с confirm.by)")
    return run


@tool("assess_release_impact",
      "Сколько стоят споры: конфликты выпуска и расхождения ПТО → рубли по ценам "
      "сметы/КС-2. Суммы считаются один раз на позицию (итерации листов не "
      "умножают спор), у каждой суммы есть формула; без цены строка идёт в «не оценено». "
      "С acts_path считается портфель: все акты объекта с покрытием проверкой и "
      "разбивкой по месяцам; непроверенный акт — «не оценено», а не ноль.",
      {"type": "object", "properties": {
          "batch_path": {"type": "string",
                         "description": "сводка выпуска (batch_summary.json)"},
          "summary_path": {"type": "string", "default": "out/summary.json"},
          "out_dir": {"type": "string", "default": "out/cad/impact"},
          "with_background": {"type": "boolean", "default": False,
                              "description": "учитывать расхождения ПТО по объекту"},
          "acts_path": {"type": "string",
                        "description": "реестр актов объекта (acts.json) — "
                                       "тогда результат по всем актам и месяцам"}},
       "required": []})
def t_assess_release_impact(args, ctx):
    if args.get("acts_path"):
        pto = json.loads(Path(args.get("summary_path") or "out/summary.json")
                         .read_text(encoding="utf-8"))
        out_dir = Path(args.get("out_dir") or "out/cad/portfolio")
        pf = assess_portfolio(load_acts(_p(args, "acts_path")), pto,
                              root=Path(ctx.get("base_dir", ".")).resolve(),
                              discrepancies_act=args.get("background_act"))
        reports = write_portfolio_reports(pf, out_dir)
        pf["reports"] = {k: str(v) if v else None for k, v in reports.items()}
        return pf
    if not args.get("batch_path"):
        raise ValueError("нужен batch_path (один выпуск) или acts_path (портфель)")
    batch = json.loads(Path(_p(args, "batch_path")).read_text(encoding="utf-8"))
    pto = json.loads(Path(args.get("summary_path") or "out/summary.json")
                     .read_text(encoding="utf-8"))
    impact = assess_impact(batch.get("conflicts") or [], pto,
                           discrepancies=(pto.get("discrepancies")
                                          if args.get("with_background") else None))
    out_dir = Path(args.get("out_dir") or "out/cad/impact")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "impact.json").write_text(
        json.dumps(impact, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_impact_xlsx(impact, out_dir / "impact.xlsx")
    chart = impact_chart(impact, out_dir / "impact_chart.png")
    impact["reports"] = {"json": str(out_dir / "impact.json"),
                         "xlsx": str(out_dir / "impact.xlsx"),
                         "chart": str(chart) if chart else None}
    return impact


@tool("scan_release_batch",
      "Выпуск целиком: 30–50 листов со сканами за один вызов. По каждому листу — "
      "распознавание, план операций и проверка по шаблону; на выходе сводка "
      "(листы, пометки, конфликты с КС-2, вопросы человеку) и файлы отчёта. "
      "Чертежи не меняются: применение операций — отдельный вызов с подтверждением.",
      {"type": "object", "properties": {
          "manifest_path": {"type": "string",
                            "description": "JSON со списком листов выпуска"},
          "engine": {"type": "string", "enum": ["auto", "vlm", "offline", "mock"],
                     "default": "vlm"},
          "out_dir": {"type": "string", "default": "out/cad/batch"},
          "summary_path": {"type": "string", "default": "out/summary.json"},
          "template_path": {"type": "string", "default": DEFAULT_TEMPLATE},
          "workers": {"type": "integer", "default": 1,
                      "description": "потоки; для engine=offline принудительно 1"},
          "resume": {"type": "boolean", "default": True,
                     "description": "брать уже обработанные листы из кеша"}},
       "required": ["manifest_path"]})
def t_scan_release_batch(args, ctx):
    base = Path(ctx.get("base_dir", "."))
    sheets = load_manifest(_p(args, "manifest_path"))
    summary_data = None
    summary_path = args.get("summary_path")
    if summary_path and Path(summary_path).exists():
        summary_data = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    summary = run_batch(sheets, engine=args.get("engine") or "vlm",
                        workers=int(args.get("workers") or 1),
                        out_dir=args.get("out_dir") or (base / "out/cad/batch"),
                        template_path=args.get("template_path") or DEFAULT_TEMPLATE,
                        summary=summary_data,
                        resume=bool(args.get("resume", True)))
    paths = write_reports(summary, args.get("out_dir") or (base / "out/cad/batch"))
    summary["reports"] = {k: str(v) for k, v in paths.items()}
    return summary


@tool("plan_from_scan_redline",
      "Скан с пометками → типизированные операции из белого списка. Каждая операция "
      "несёт source.ref (номер пометки) и reason. Пометки без текста, с низкой "
      "уверенностью и с конфликтом против КС-2/журнала уходят в needs_human_decision.",
      {"type": "object", "properties": {
          "scan_path": {"type": "string"},
          "file_path": {"type": "string"},
          "summary_path": {"type": "string", "description": "out/summary.json ПТО"},
          "engine": {"type": "string", "enum": ["auto", "vlm", "offline", "mock"],
                     "default": "vlm"},
          "texts_path": {"type": "string"},
          "truth_path": {"type": "string"},
          "provider": {"type": "string"},
          "markup_layer": {"type": "string", "default": "ПОМЕТКИ"},
          "template_path": {"type": "string", "default": DEFAULT_TEMPLATE}},
       "required": ["scan_path", "file_path"]})
def t_plan_from_scan_redline(args, ctx):
    d = Drawing.open(_p(args, "file_path"))
    tpl = load_template(args.get("template_path") or DEFAULT_TEMPLATE)
    kw = {"engine": args.get("engine") or "vlm",
          "log_path": Path(ctx.get("base_dir", ".")) / "out/cad/llm_calls.jsonl"}
    for src, dst in (("texts_path", "texts_path"), ("truth_path", "truth_path"),
                     ("provider", "provider_id")):
        if args.get(src):
            kw[dst] = args[src]
    run = analyze_scan(_p(args, "scan_path"), drawing=d, **kw)
    summary = None
    if args.get("summary_path") and Path(args["summary_path"]).exists():
        summary = json.loads(Path(args["summary_path"]).read_text(encoding="utf-8"))
    plan = plan_from_scan(run, d, tpl, summary,
                          markup_layer=args.get("markup_layer") or "ПОМЕТКИ")
    plan["recognition_stats"] = run.get("stats")
    plan["recognition_warnings"] = run.get("warnings")
    plan["validation"] = validate_drawing(d, tpl)
    return plan


@tool("validate_operations",
      "Проверка плана операций: белый список типов, защищённые слои, высота текста, "
      "лимиты полилиний, поля штампа, порог уверенности.",
      {"type": "object", "properties": {
          "file_path": {"type": "string"},
          "operations": {"type": "array", "items": {"type": "object"}},
          "template_path": {"type": "string", "default": DEFAULT_TEMPLATE}},
       "required": ["file_path", "operations"]})
def t_validate_operations(args, ctx):
    d = Drawing.open(_p(args, "file_path"))
    tpl = load_template(args.get("template_path") or DEFAULT_TEMPLATE)
    issues = validate_operations(d.doc, args.get("operations") or [], tpl)
    blocking = [i for i in issues if i["severity"] == "blocking"]
    return {"ok": not blocking, "blocking": len(blocking), "warnings": len(issues) - len(blocking),
            "issues": issues}


@tool("apply_operations",
      "Применение операций к чертежу. mode=dry_run возвращает diff без записи файла. "
      "mode=commit требует подтверждения человека (confirm.by) и пишет новую версию файла.",
      {"type": "object", "properties": {
          "file_path": {"type": "string"},
          "operations": {"type": "array", "items": {"type": "object"}},
          "mode": {"type": "string", "enum": ["dry_run", "commit"], "default": "dry_run"},
          "out_path": {"type": "string", "description": "куда сохранить новую версию (commit)"},
          "confirm": {"type": "object", "properties": {
              "by": {"type": "string"}, "role": {"type": "string"},
              "comment": {"type": "string"}}},
          "template_path": {"type": "string", "default": DEFAULT_TEMPLATE}},
       "required": ["file_path", "operations"]})
def t_apply_operations(args, ctx):
    d = Drawing.open(_p(args, "file_path"))
    tpl = load_template(args.get("template_path") or DEFAULT_TEMPLATE)
    mode = args.get("mode") or "dry_run"
    audit = AuditLog(ctx["audit_path"])
    res = apply_operations(d, args.get("operations") or [], tpl, mode=mode,
                           out_path=args.get("out_path"), approval=args.get("confirm"),
                           audit=audit if mode == "commit" else None,
                           actor=(args.get("confirm") or {}).get("by") or "agent:cad")
    if mode == "commit" and res.get("ok"):
        _append_history(ctx, history_entry(res, d, tpl, args.get("confirm"),
                                           (args.get("confirm") or {}).get("by") or "agent:cad"))
    return res


def _append_history(ctx: dict, entry: dict) -> dict:
    """Дописать запись истории изменений чертежа (тот же формат, что у CLI).

    Раньше эта функция вызывалась, но не была определена: файл и запись аудита
    создавались, а клиент получал NameError с трейсбеком.
    """
    path = Path(ctx.get("history_path") or (Path(ctx.get("base_dir") or ".") /
                                            "out" / "cad" / "history.jsonl"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    entry = dict(entry)
    entry["journal"] = str(path)
    return entry


@tool("cad_takeoff",
      "Объёмы, проставленные на схеме, и их сверка с объёмами акта (±5 % по ТЗ).",
      {"type": "object", "properties": {
          "file_path": {"type": "string"},
          "summary_path": {"type": "string", "description": "summary.json прогона ПТО"},
          "tolerance_pct": {"type": "number", "default": 5}},
       "required": ["file_path"]})
def t_takeoff(args, ctx):
    d = Drawing.open(_p(args, "file_path"))
    items = takeoff(d)
    out = {"positions": items, "count": len(items)}
    if args.get("summary_path"):
        summary = json.loads(Path(args["summary_path"]).read_text(encoding="utf-8"))
        lines = summary.get("lines") or []
        out["comparison"] = compare_takeoff(items, lines,
                                            pct=float(args.get("tolerance_pct") or 5))
    return out


@tool("review_summary",
      "Сводка проверки для инженера: что проверено и с каким итогом, что осталось открытым, "
      "где нужно решение человека, сколько денег под риском и можно ли подписывать. "
      "Собирает summary.json, сводку выпуска, план правок, деньги и реестр актов в один ответ; "
      "отсутствующие артефакты помечаются как «не выполнялось», а не как «всё хорошо».",
      {"type": "object", "properties": {
          "summary_path": {"type": "string", "description": "out/summary.json (сверка КС-2)"},
          "batch_path": {"type": "string", "description": "сводка выпуска batch_summary.json"},
          "acts_path": {"type": "string", "description": "реестр актов acts.json"},
          "impact_path": {"type": "string", "description": "impact.json (деньги под риском)"},
          "plan_path": {"type": "string", "description": "план операций по чертежу"},
          "materials_path": {"type": "string",
                             "description": "сверка материалов materials.json (заявка → приход → КС-2)"},
          "object_name": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json",
                     "description": "markdown — готовый текст для отчёта"}},
       "required": []})
def t_review_summary(args, ctx):
    base = Path(ctx.get("base_dir") or ".")
    def p(key, default):
        value = args.get(key)
        return Path(value) if value else base / default
    review = collect_review(summary_path=p("summary_path", "out/summary.json"),
                            batch_path=p("batch_path", "out/cad/batch/batch_summary.json"),
                            acts_path=p("acts_path", "data/demo/acts/acts.json"),
                            impact_path=p("impact_path", "out/cad/impact/impact.json"),
                            plan_path=p("plan_path", "out/cad/plan.json"),
                            materials_path=p("materials_path", "out/materials/materials.json"),
                            object_name=args.get("object_name"))
    if (args.get("format") or "json") == "markdown":
        return {"ok": True, "format": "markdown", "markdown": review_to_markdown(review),
                "verdict": review["verdict"]}
    return {"ok": True, **review}


@tool("asbuilt_docs",
      "Исполнительная документация: черновик АОСР по действующей форме (приказ Минстроя "
      "№ 344/пр), исполнительная схема из DXF, реестр ИД и комплектность по матрице видов "
      "работ (электромонтаж: изоляция, петля «фаза-нуль», УЗО), план отдельных актов по "
      "скрытым работам (демонтаж и вывоз мусора в акты не попадают) и сверка норм с "
      "нормативной базой (непроверенная ссылка так и называется). Плюс проверки черновика: "
      "хронология дат, нормы только с пунктами, ссылка на РД, подписи и НРС. Готовит ЧЕРНОВИК: "
      "подписывает человек, формулировка «готов к подписанию» не используется.",
      {"type": "object", "properties": {
          "kind": {"type": "string",
                   "enum": ["scheme", "aosr", "registry", "check", "requirements",
                            "validate", "spec", "forms", "acts", "verify-norms", "smeta"],
                   "description": "scheme — схема из DXF; aosr — черновик акта скрытых работ; "
                                  "registry — реестр ИД (XLSX); check — комплектность; "
                                  "requirements — матрица требований по видам работ; "
                                  "validate — проверки черновика (даты, пункты норм, подписи); "
                                  "spec — спецификация АОСР из сметы/КС-2; forms — какие формы действуют; "
                                  "acts — план отдельных актов по скрытым работам; "
                                  "verify-norms — сверка норм с нормативной базой; "
                                  "smeta — разбор сметы АРОС (md/html) или xlsx: работы, "
                                  "материалы, классификация"},
          "drawing_path": {"type": "string", "description": "DXF для схемы"},
          "spec_path": {"type": "string", "description": "JSON/YAML со данными акта или реестра"},
          "spec": {"type": "object", "description": "те же данные inline, вместо spec_path"},
          "out_dir": {"type": "string", "description": "куда класть документы (по умолчанию out/asbuilt)"},
          "object_name": {"type": "string"},
          "work": {"type": "string", "description": "вид работ на схеме"},
          "code": {"type": "string", "description": "шифр схемы"},
          "act_ref": {"type": "string", "description": "акт КС-2"},
          "executor": {"type": "string"}, "reviewer": {"type": "string"},
          "organization": {"type": "string"}, "drawing_no": {"type": "string"},
          "date": {"type": "string"},
          "require": {"type": "array", "items": {"type": "string"},
                      "description": "обязательные типы документов для check"},
          "work_types": {"type": "array", "items": {"type": "string"},
                         "description": "виды работ: concrete, electrical, roofing … "
                                        "включают матрицу требований"},
          "form": {"type": "string", "enum": ["344pr", "rd-11-02-2006"],
                   "description": "форма акта; по умолчанию 344pr (действует с 01.09.2023)"},
          "smeta_path": {"type": "string", "description": "смета XLSX для kind=spec"},
          "summary_source": {"type": "string", "description": "out/summary.json для kind=spec"},
          "act_number": {"type": "string"}, "start_date": {"type": "string"},
          "end_date": {"type": "string"},
          "act_prefix": {"type": "string", "description": "префикс нумерации актов для kind=acts"},
          "write_specs": {"type": "boolean", "default": False,
                          "description": "для kind=acts: вернуть спецификации по каждому акту"},
          "norms_dir": {"type": "string", "description": "нормативная база для verify-norms"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": ["kind"]})
def t_asbuilt_docs(args, ctx):
    base = Path(ctx.get("base_dir") or ".")
    out = Path(args.get("out_dir") or base / "out/asbuilt")
    kind = (args.get("kind") or "").lower()
    markdown = (args.get("format") or "json") == "markdown"

    work_types = args.get("work_types") or []

    if kind == "forms":
        return {"ok": True, **forms_catalog()}

    if kind == "requirements":
        if work_types:
            return work_type_requirements(work_types[0])
        return {"ok": True, "markdown": matrix_summary()}

    if kind == "spec":
        positions, source = load_positions(smeta=args.get("smeta_path"),
                                           summary_path=args.get("summary_source"))
        spec = spec_from_estimate(positions, object_name=args.get("object_name") or "",
                                  act_number=args.get("act_number") or "",
                                  act_date=args.get("date") or "",
                                  start_date=args.get("start_date") or "",
                                  end_date=args.get("end_date") or "",
                                  work=args.get("work") or "",
                                  work_types=work_types or None, source=source)
        return {"ok": True, "spec": spec, "markdown": spec_to_markdown(spec),
                "needs_input": spec["needs_input"]}

    if kind == "smeta":
        from ..asbuilt.aros import aros_to_markdown, read_aros
        from ..asbuilt.requirements import classify_positions
        positions, source = load_positions(smeta=args.get("smeta_path"),
                                           summary_path=args.get("summary_source"))
        path = Path(args.get("smeta_path") or "")
        parsed = read_aros(path) if path.suffix.lower() in {".md", ".markdown", ".html",
                                                            ".htm"} else None
        classified = classify_positions(positions)
        payload = {"ok": True, "source": source,
                   "classification": {
                       "total": classified["total"],
                       "classified": classified["classified"],
                       "materials_skipped": classified.get("materials_skipped"),
                       "work_types": [{"work_type": w["work_type"], "positions": len(w["positions"]),
                                       "share": w.get("share")}
                                      for w in classified["work_types"]],
                       "candidates": [{"work_type": w["work_type"],
                                       "positions": len(w["positions"])}
                                      for w in classified["candidates"]],
                       "unknown": classified["unknown"]}}
        if parsed:
            payload["sections"] = [{"index": s["index"], "title": s["title"]}
                                   for s in parsed["sections"]]
            payload["works"], payload["materials"] = len(parsed["works"]), len(parsed["materials"])
            payload["markdown"] = aros_to_markdown(parsed)
        return payload

    if kind == "acts":
        positions, source = load_positions(smeta=args.get("smeta_path"),
                                           summary_path=args.get("summary_source"))
        plan = plan_acts(positions, work_types=work_types or None,
                         act_prefix=args.get("act_prefix") or "АОСР",
                         object_name=args.get("object_name") or "", source=source,
                         act_date=args.get("date"))
        payload = {"ok": True, "plan": plan, "markdown": plan_to_markdown(plan)}
        if args.get("write_specs"):
            payload["specs"] = plan_to_specs(plan)
        return payload

    if kind == "verify-norms":
        spec = args.get("spec") or {}
        if args.get("spec_path"):
            spec = _load_doc_spec(Path(args["spec_path"]))
        report = verify_norms(spec.get("norms") or [], args.get("norms_dir"))
        return {"ok": True, "report": report, "markdown": norms_to_markdown(report),
                "verdict": report["verdict"]}

    if kind == "scheme":
        drawing = args.get("drawing_path")
        if not drawing:
            raise ValueError("для kind=scheme нужен drawing_path")
        result = build_scheme(Path(drawing), out,
                              object_name=args.get("object_name") or "",
                              work=args.get("work") or "",
                              code=args.get("code") or "",
                              act_ref=args.get("act_ref") or "",
                              executor=args.get("executor") or "",
                              reviewer=args.get("reviewer") or "",
                              organization=args.get("organization") or "",
                              drawing_no=args.get("drawing_no") or "",
                              scheme_date=args.get("date"))
        if markdown:
            return {"ok": True, "format": "markdown", "markdown": scheme_to_markdown(result)}
        return {"ok": True, **result}

    spec = args.get("spec")
    if spec is None:
        path = args.get("spec_path")
        if not path:
            raise ValueError("нужен spec или spec_path")
        spec = _load_doc_spec(Path(path))

    if work_types and not spec.get("work_types"):
        spec["work_types"] = list(work_types)

    if kind == "validate":
        draft = validate_draft(spec, work_types=spec.get("work_types") or None)
        return {"ok": True, **draft}

    if kind == "aosr":
        result = build_aosr(spec, out, form=args.get("form"))
        if markdown:
            return {"ok": True, "format": "markdown", "markdown": aosr_to_markdown(result),
                    "can_transfer": result["can_transfer"], "status": result["status"]}
        return {"ok": True, **result}
    if kind == "registry":
        result = build_registry(spec, out)
        if markdown:
            return {"ok": True, "format": "markdown", "markdown": registry_to_markdown(result),
                    "complete": result["completeness"]["complete"]}
        return {"ok": True, **result}
    if kind == "check":
        comp = check_completeness(spec, required=args.get("require") or None,
                                  act_ref=args.get("act_ref") or None,
                                  work_types=work_types or None)
        return {"ok": True, **comp}
    raise ValueError(f"неизвестный kind: {kind}")


@tool("external_takeoff",
      "Обмер чужим MCP-сервером (PDF, экспорт из BIM, IFC): вызов по конфигу, приведение "
      "ответа к нашим позициям и сверка с нашим обмером или сметой. Код внешнего сервера "
      "не встраивается — только вызов по протоколу; для AGPL-обмеров нужен режим "
      "«отдельный сервис» (ack_separate_service).",
      {"type": "object", "properties": {
          "config_path": {"type": "string",
                          "description": "JSON/YAML: транспорт, команда/url, инструмент, лицензия"},
          "file_path": {"type": "string", "description": "файл, который мерит внешний сервер"},
          "drawing_path": {"type": "string", "description": "наш DXF для сверки объёмов"},
          "summary_path": {"type": "string", "description": "out/summary.json для сверки со сметой"},
          "tolerance_pct": {"type": "number", "default": 5},
          "ack_separate_service": {"type": "boolean", "default": False,
                                   "description": "подтверждение режима «отдельный сервис» (AGPL)"}},
       "required": ["config_path", "file_path"]})
def t_external_takeoff(args, ctx):
    cfg = load_config(_p(args, "config_path"))
    report = run_external_takeoff(cfg, _p(args, "file_path"),
                                  ack_separate_service=bool(args.get("ack_separate_service")))
    local = None
    if args.get("drawing_path"):
        local = local_positions_from_dxf(Drawing.open(_p(args, "drawing_path")))
    elif args.get("summary_path") and Path(args["summary_path"]).exists():
        local = local_positions_from_summary(
            json.loads(Path(args["summary_path"]).read_text(encoding="utf-8")))
    if local is not None:
        report["alignment"] = align(report["positions"], local,
                                    pct=float(args.get("tolerance_pct") or 5))
    return report


@tool("cad_diff",
      "Сравнение двух версий чертежа: добавленные, изменённые и удалённые объекты по слоям.",
      {"type": "object", "properties": {
          "before_path": {"type": "string"}, "after_path": {"type": "string"}},
       "required": ["before_path", "after_path"]})
def t_diff(args, ctx):
    a = Drawing.open(_p(args, "before_path"))
    b = Drawing.open(_p(args, "after_path"))
    res = diff_drawings(a.doc, b.doc)
    res.update({"before": str(a.path), "after": str(b.path),
                "sha256_before": a.sha256, "sha256_after": b.sha256})
    return res


@tool("export_scheme",
      "Выпуск исполнительной схемы: новая версия DXF + растровый предпросмотр (png) + "
      "паспорт выпуска (json). PDF требует PyMuPDF и в текущей сборке недоступен.",
      {"type": "object", "properties": {
          "file_path": {"type": "string"},
          "out_dir": {"type": "string", "default": "out/cad"},
          "stamp": {"type": "string", "description": "текст штампа выпуска, напр. «Черновик»"},
          "confirm": {"type": "object"}},
       "required": ["file_path"]})
def t_export_scheme(args, ctx):
    src = Drawing.open(_p(args, "file_path"))
    out_dir = Path(args.get("out_dir") or "out/cad")
    out_dir.mkdir(parents=True, exist_ok=True)
    dxf_out = out_dir / (src.path.stem + "_scheme.dxf")
    png_out = out_dir / (src.path.stem + "_scheme.png")
    saved = src.save_as(dxf_out)
    render_png(saved, png_out)
    manifest = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": {"file": str(src.path), "sha256": src.sha256},
        "scheme_dxf": {"file": str(dxf_out), "sha256": saved.sha256},
        "preview_png": str(png_out),
        "stamp": args.get("stamp") or "Черновик исполнительной схемы",
        "confirm": args.get("confirm"),
        "note": "PDF не формируется: нет PyMuPDF; подпись и передача заказчику — только человеком",
    }
    (out_dir / (src.path.stem + "_scheme.manifest.json")).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    AuditLog(ctx["audit_path"]).append(
        (args.get("confirm") or {}).get("by") or "agent:cad", "agent", "cad.scheme.export",
        str(dxf_out), {"manifest": manifest["scheme_dxf"], "preview": str(png_out)})
    return manifest


@tool("cad_list_operations",
      "Белый список операций CAD и их параметры (что агенту разрешено делать с чертежом).",
      {"type": "object", "properties": {}})
def t_list_operations(args, ctx):
    return {"operations": [{"type": k, "label": v["label"], "required_params": v["params"]}
                           for k, v in OPERATIONS.items()],
            "forbidden": ["DELETE_ENTITY", "MODIFY_LAYER", "ADD_RASTER", "ROTATE_ENTITY",
                          "COPY_ENTITY", "SCALE_ENTITY"],
            "min_confidence": 0.7,
            "human_required_for": ["commit", "штамп: поле ФИО", "выпуск схемы заказчику"]}


@tool("cad_history",
      "Журнал изменений чертежей: версии, хэши, операции, кто подтвердил.",
      {"type": "object", "properties": {
          "history_path": {"type": "string", "default": "out/cad/history.jsonl"},
          "limit": {"type": "integer", "default": 20}}})
def t_history(args, ctx):
    path = Path(args.get("history_path") or "out/cad/history.jsonl")
    if not path.exists():
        return {"entries": [], "note": f"журнал не найден: {path}"}
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return {"entries": lines[-int(args.get("limit") or 20):][::-1], "total": len(lines)}


# ─────────────────────────── JSON-RPC ────────────────────────────────────
def _public_message(exc: Exception) -> str:
    """Текст ошибки без путей файловой системы и без деталей окружения."""
    text = str(exc)
    text = re.sub(r"(/[\w.\-]+){2,}", "<путь>", text)          # POSIX-пути
    text = re.sub(r"[A-Za-z]:\\[^\s:]+", "<путь>", text)       # Windows-пути
    return text[:300] or type(exc).__name__


def _tools_public():
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
            for t in TOOLS]


def _find_tool(name):
    return next((t for t in TOOLS if t["name"] == name), None)


def handle_rpc(req: dict, ctx: dict) -> dict | None:
    """Один JSON-RPC запрос → ответ (или None для нотификаций)."""
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": ("CAD-инструменты агента ПТО: чтение DXF, проверка шаблона, "
                             "пометки по расхождениям, применение операций только с "
                             "подтверждением человека."),
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": _tools_public()})
    if method == "tools/call":
        name = params.get("name")
        t = _find_tool(name)
        if t is None:
            return err(-32602, f"неизвестный инструмент: {name}")
        try:
            arguments = params.get("arguments") or {}
            check_paths(arguments, ctx)          # песочница до выполнения инструмента
            result = t["fn"](arguments, ctx)
            payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
            is_error = bool(isinstance(result, dict) and result.get("ok") is False)
            return ok({"content": [{"type": "text", "text": payload}], "isError": is_error})
        except Exception as exc:
            # клиенту — короткая ошибка с идентификатором; трейсбек и абсолютные пути
            # остаются в stderr сервера (иначе они утекают наружу вместе с текстом)
            error_id = uuid.uuid4().hex[:8]
            sys.stderr.write(f"[{error_id}] tools/call {name}: {traceback.format_exc()}\n")
            return ok({"content": [{"type": "text",
                                    "text": f"{type(exc).__name__}: {_public_message(exc)} "
                                            f"(id {error_id})"}],
                       "isError": True})
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return err(-32601, f"метод не поддерживается: {method}")


# Ключи аргументов, которые являются путями ЗАПИСИ: писать вне корня нельзя никогда.
WRITE_PATH_KEYS = {"out_path", "out_dir", "history_path", "audit_path", "output_path",
                   "target_path", "save_path", "xlsx_path"}


def safe_path(value, base_dir: str | Path, *, write: bool = False) -> Path:
    """Разрешить путь внутри корня; выход за корень — отказ, а не «попробуем».

    Читать файл вне `base_dir` агенту не нужно (он работает с проектом), а запись вне
    корня — это запись в чужие каталоги от имени сервиса. Отдельно: клиент не должен
    получать в ответе абсолютные пути файловой системы.
    """
    base = Path(base_dir).resolve()
    raw = Path(str(value)).expanduser()
    resolved = (base / raw).resolve() if not raw.is_absolute() else raw.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"путь вне рабочего каталога запрещён: {raw.name}")
    if write:
        parent = resolved.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
    return resolved


def check_paths(arguments: dict, ctx: dict) -> None:
    """Проверить аргументы-пути вызова. Бросает ValueError — до выполнения инструмента.

    Граница безопасности — транспорт. Для `http` (сервис в сети) песочница включена
    всегда; для `stdio` (локальный клиент того же пользователя) её надо включить явно:
    `PTO_MCP_SANDBOX=1` или `make_ctx(..., sandbox=True)`.
    """
    base = ctx.get("base_dir") or "."
    force = os.environ.get("PTO_MCP_SANDBOX") == "1"
    sandbox_writes = bool(ctx.get("sandbox_writes")) or force
    sandbox_reads = bool(ctx.get("sandbox_reads")) or force
    for key, value in (arguments or {}).items():
        if not isinstance(value, str) or not value:
            continue
        if not (key.endswith("_path") or key in WRITE_PATH_KEYS):
            continue
        if key in WRITE_PATH_KEYS:
            if sandbox_writes:
                safe_path(value, base, write=True)
        elif sandbox_reads:
            safe_path(value, base)


def make_ctx(base_dir: str | Path = ".", audit_path: str | Path = "out/cad/audit.jsonl",
             *, sandbox: bool = False, token: str | None = None,
             allow_origin: str | None = None) -> dict:
    base = Path(base_dir)
    return {"base_dir": str(base), "audit_path": str(base / audit_path),
            "history_path": str(base / "out" / "cad" / "history.jsonl"),
            "sandbox_reads": sandbox, "sandbox_writes": sandbox,
            "token": token, "allow_origin": allow_origin}


# ─────────────────────────── транспорты ──────────────────────────────────
def serve_stdio(ctx):
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "invalid JSON"}}), flush=True)
            continue
        resp = handle_rpc(req, ctx)
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _resolve_http_security(ctx, host: str, token: str | None) -> tuple[str, str]:
    """Решить, можно ли вообще поднимать HTTP и какой токен требовать.

    Правила: (1) по умолчанию слушаем только localhost — сетевой доступ включается
    осознанно; (2) без токена наружу не выходим; (3) нет токена в окружении — генерируем
    случайный и печатаем его в stderr (клиент без токена не пройдёт).
    """
    token = token or ctx.get("token") or os.environ.get("PTO_MCP_TOKEN")
    loopback = host in LOOPBACK_HOSTS
    if not loopback and not token:
        raise SystemExit(
            f"отказ: --host {host} выставляет инструменты в сеть, но токен не задан. "
            f"Задайте PTO_MCP_TOKEN (или --token) либо слушайте 127.0.0.1.")
    if token is None:
        token = secrets.token_urlsafe(24)
        sys.stderr.write(f"токен не задан — сгенерирован на этот запуск: {token}\n"
                         f"передайте его клиенту в заголовке Authorization: Bearer <токен>\n")
    return token, ctx.get("allow_origin") or os.environ.get("PTO_MCP_ALLOW_ORIGIN") or ""


def serve_http(ctx, host="127.0.0.1", port=8765, token: str | None = None):
    token, allow_origin = _resolve_http_security(ctx, host, token)
    # HTTP-транспорт: сервис доступен по сети, поэтому песочница путей включена всегда
    ctx = dict(ctx, token=token, allow_origin=allow_origin,
               sandbox_reads=True, sandbox_writes=True)

    class Handler(BaseHTTPRequestHandler):
        server_version = "pto-cad-mcp/0.1"

        def _cors(self):
            # CORS только для явно разрешённого origin; «*» не отдаём никогда —
            # иначе любая страница в браузере может дёргать инструменты
            if allow_origin:
                self.send_header("Access-Control-Allow-Origin", allow_origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers",
                                 "content-type, mcp-protocol-version, authorization")
                self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization") or ""
            presented = header[7:].strip() if header.lower().startswith("bearer ") else ""
            if not presented:
                presented = self.headers.get("X-PTO-Token") or ""
            return bool(presented) and hmac.compare_digest(presented, token)

        def log_message(self, fmt, *a):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

        def do_OPTIONS(self):
            if allow_origin and (self.headers.get("Origin") or "") != allow_origin:
                return self._send(403, b'{"error":"origin not allowed"}')
            self._send(204, b"")

        def do_GET(self):
            if self.path.startswith("/health"):
                return self._send(200, json.dumps({"status": "ok", "tools": len(TOOLS)}).encode())
            if self.path.startswith("/tools"):
                return self._send(200, json.dumps({"tools": _tools_public()},
                                                  ensure_ascii=False).encode())
            if self.path in ("/", "/index.html"):
                if not self._authorized():
                    return self._send(401, b'{"error":"unauthorized"}')
                return self._send(200, _inspector_html(ctx).encode("utf-8"),
                                  "text/html; charset=utf-8")
            self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if not self._authorized():
                return self._send(401, b'{"error":"unauthorized"}')
            if allow_origin:
                origin = self.headers.get("Origin") or ""
                if origin and origin != allow_origin:
                    return self._send(403, b'{"error":"origin not allowed"}')
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                req = json.loads(raw)
            except json.JSONDecodeError:
                return self._send(400, b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"invalid JSON"}}')
            if isinstance(req, list):
                resp = [r for r in (handle_rpc(x, ctx) for x in req) if r is not None]
            else:
                resp = handle_rpc(req, ctx)
            if resp is None:
                return self._send(202, b"")
            self._send(200, json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"MCP (http) слушает http://{host}:{port}/mcp · инспектор: http://{host}:{port}/ · "
          f"токен: {'требуется' if token else 'нет'} · CORS: {allow_origin or 'закрыт'}",
          flush=True)
    srv.serve_forever()


def _inspector_html(ctx) -> str:
    tools = _tools_public()
    cards = "".join(
        f"<div class='tool'><div class='tn'>{t['name']}</div>"
        f"<div class='td'>{t['description']}</div>"
        f"<details><summary>inputSchema</summary><pre>{json.dumps(t['inputSchema'], ensure_ascii=False, indent=1)}</pre></details></div>"
        for t in tools)
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCP-сервер CAD · ИИ-агент ПТО</title>
<style>
 body{{margin:0;font:15px/1.55 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:#F4F6FA;color:#1E2430}}
 .top{{background:#1F3864;color:#fff;padding:20px 28px}}
 .top h1{{margin:0;font-size:20px}} .top .m{{color:#B8C7DF;font-size:13px;margin-top:4px}}
 .wrap{{max-width:1080px;margin:0 auto;padding:22px 28px 60px}}
 h2{{font-size:16px;color:#1F3864;margin:22px 0 8px}}
 .grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
 .tool{{background:#fff;border:1px solid #DCE3EE;border-radius:9px;padding:12px 14px}}
 .tn{{font-family:Consolas,monospace;font-weight:700;color:#1F3864}}
 .td{{font-size:13px;color:#5A6474;margin:4px 0 6px}}
 pre{{background:#F7F9FC;border:1px solid #E6EBF3;border-radius:6px;padding:8px;font-size:11.5px;overflow:auto;max-height:220px}}
 textarea{{width:100%;height:130px;font-family:Consolas,monospace;font-size:12.5px;
          border:1px solid #DCE3EE;border-radius:8px;padding:10px}}
 button{{background:#1F3864;color:#fff;border:0;border-radius:7px;padding:9px 16px;font-size:14px;cursor:pointer}}
 .out{{background:#0F1B2D;color:#D7E3F4;border-radius:8px;padding:12px;font-family:Consolas,monospace;
       font-size:12px;white-space:pre-wrap;max-height:320px;overflow:auto;margin-top:10px}}
 .hint{{color:#6B7688;font-size:12.5px}}
</style></head><body>
<div class="top"><h1>MCP-сервер CAD · ИИ-агент ПТО</h1>
<div class="m">протокол {PROTOCOL_VERSION} · инструментов {len(tools)} · транспорт HTTP JSON-RPC: <b>POST /mcp</b></div></div>
<div class="wrap">
 <h2>Инструменты</h2>
 <div class="grid">{cards}</div>
 <h2>Проверка вызова</h2>
 <p class="hint">Запрос уходит на <code>POST /mcp</code>. Пример: <code>{{"jsonrpc":"2.0","id":1,"method":"tools/list"}}</code></p>
 <textarea id="q">{{"jsonrpc":"2.0","id":1,"method":"tools/list"}}</textarea>
 <div><label class="hint">Токен доступа: <input id="tok" placeholder="PTO_MCP_TOKEN" size="34"></label></div>
 <button onclick="send()">Отправить</button>
 <div class="out" id="out">—</div>
</div>
<script>
async function send(){{
  const q=document.getElementById('q').value, out=document.getElementById('out');
  out.textContent='…';
  try{{
    const tok=(document.getElementById('tok')||{{}}).value||'';
    const r=await fetch('mcp',{{method:'POST',
      headers:{{'Content-Type':'application/json',...(tok?{{'Authorization':'Bearer '+tok}}:{{}})}},
      body:q}});
    const t=await r.text(); try{{out.textContent=JSON.stringify(JSON.parse(t),null,2);}}catch(e){{out.textContent=t;}}
  }}catch(e){{out.textContent='ошибка: '+e;}}
}}
</script></body></html>"""


@tool("prices",
      "Цены ресурсов с источником и датой: импорт выгрузки (ФГИС ЦС, письмо индексов, прайс "
      "поставщика) как новой версии, поиск цены на дату, история изменений. Если цены нет — "
      "так и говорит (`found: false`) и перечисляет, в каких версиях смотрел: подставлять "
      "«похожий» ресурс агент не будет.",
      {"type": "object", "properties": {
          "action": {"type": "string", "enum": ["import", "lookup", "history", "list"],
                     "description": "import — загрузить выгрузку; lookup — цена на дату; "
                                    "history — как менялась; list — версии в базе"},
          "file_path": {"type": "string", "description": "выгрузка цен (xlsx/csv) для import"},
          "source": {"type": "string", "description": "откуда цены: ФГИС ЦС, письмо индексов, "
                                                     "прайс поставщика"},
          "price_date": {"type": "string", "description": "дата, на которую действуют цены"},
          "code": {"type": "string"}, "name": {"type": "string"},
          "as_of": {"type": "string", "description": "искать цену на эту дату"},
          "region": {"type": "string"}, "zone": {"type": "string"},
          "prices_dir": {"type": "string", "default": "data/prices"},
          "audit_path": {"type": "string", "description": "журнал аудита (по умолчанию не пишем)"}},
       "required": ["action"]})
def t_prices(args, ctx):
    from ..prices import import_prices, load_index, lookup, price_history, prices_to_markdown
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("prices_dir") or base / "data" / "prices")
    action = args.get("action")
    audit = None
    if args.get("audit_path"):
        from ..audit import AuditLog
        audit = AuditLog(Path(args["audit_path"]))
    if action == "import":
        result = import_prices(Path(_p(args, "file_path")), source=_p(args, "source"),
                               price_date=_p(args, "price_date"),
                               region=args.get("region") or "", zone=args.get("zone") or "",
                               out_dir=out_dir, audit_log=audit)
        return result
    if action == "lookup":
        result = lookup(code=args.get("code") or "", name=args.get("name") or "",
                        as_of=args.get("as_of"), out_dir=out_dir)
        return {"ok": True, "price": result, "markdown": prices_to_markdown(lookup_result=result)}
    if action == "history":
        history = price_history(code=args.get("code") or "", name=args.get("name") or "",
                                out_dir=out_dir)
        return {"ok": True, "history": history, "markdown": prices_to_markdown(history=history)}
    index = load_index(out_dir)
    return {"ok": True, "index": index, "markdown": prices_to_markdown(index=index)}


@tool("procurement",
      "Закупка материалов: заявка из сметы (в т.ч. выгрузки АРОС), сравнение коммерческих "
      "предложений с приведением к цене с НДС и обоснованием выбора. КП без даты, срока "
      "поставки или срока действия цены в сравнении не участвует — уходит в `not_comparable` "
      "с причиной. Выбор поставщика за человеком: `decide` пишет его в журнал аудита "
      "с автором и причиной.",
      {"type": "object", "properties": {
          "action": {"type": "string", "enum": ["request", "compare", "decide"],
                     "description": "request — заявка из сметы; compare — сравнить КП; "
                                    "decide — зафиксировать решение человека"},
          "smeta_path": {"type": "string", "description": "смета xlsx или выгрузка АРОС (md/html)"},
          "request_path": {"type": "string", "description": "JSON заявки (для compare/decide)"},
          "quotes_paths": {"type": "array", "items": {"type": "string"},
                           "description": "файлы КП (csv/xlsx), можно несколько"},
          "prefer": {"type": "string", "enum": ["price", "delivery"], "default": "price"},
          "choose": {"type": "object", "description": "для decide: {код ресурса: поставщик}"},
          "by": {"type": "string", "description": "кто решил (ФИО)"},
          "role": {"type": "string", "default": "инженер ПТО"},
          "reason": {"type": "string", "description": "почему выбран этот поставщик"},
          "object_name": {"type": "string"}, "request_no": {"type": "string"},
          "need_by": {"type": "string"},
          "audit_path": {"type": "string"}},
       "required": ["action"]})
def t_procurement(args, ctx):
    from ..procurement import (compare_quotes, decide, load_quotes, procurement_to_markdown,
                               request_from_estimate)
    action = args.get("action")
    if action == "request":
        request = request_from_estimate(Path(_p(args, "smeta_path")),
                                        object_name=args.get("object_name") or "",
                                        request_no=args.get("request_no") or "",
                                        need_by=args.get("need_by") or "")
        return {"ok": True, "request": request,
                "markdown": procurement_to_markdown(
                    {"request_no": request["request_no"], "status": "request",
                     "lines": [], "lines_count": 0, "amount": 0, "saving_vs_worst": 0},
                    request=request)}

    request = json.loads(Path(_p(args, "request_path")).read_text(encoding="utf-8"))
    quotes, problems = [], []
    for path in args.get("quotes_paths") or []:
        loaded, issues = load_quotes(Path(path))
        quotes += loaded
        problems += issues
    comparison = compare_quotes(request, quotes, prefer=args.get("prefer") or "price")
    payload = {"ok": True, "comparison": comparison, "read_problems": problems,
               "markdown": procurement_to_markdown(comparison, request=request)}
    if action == "decide":
        audit = None
        if args.get("audit_path"):
            from ..audit import AuditLog
            audit = AuditLog(Path(args["audit_path"]))
        decision = decide(comparison, args.get("choose") or {}, by=_p(args, "by"),
                          role=args.get("role") or "инженер ПТО",
                          reason=args.get("reason") or "", audit_log=audit)
        payload["decision"] = decision
        payload["markdown"] = procurement_to_markdown(comparison, decision=decision,
                                                     request=request)
    return payload


@tool("audit_trail",
      "Журнал аудита: лента изменений (кто, когда, что изменил, почему), запись по хэшу, "
      "проверка целостности хэш-цепочки. Изменения пишутся со снимком «до/после» и дельтой — "
      "видно не только факт, но и что именно поменялось и на каком основании.",
      {"type": "object", "properties": {
          "action": {"type": "string", "enum": ["list", "show", "verify"], "default": "list"},
          "audit_path": {"type": "string", "default": "out/audit.jsonl"},
          "entity": {"type": "string", "description": "фильтр по сущности, напр. procurement:ЗМ-1:код"},
          "hash": {"type": "string", "description": "начало хэша записи для action=show"},
          "limit": {"type": "integer", "description": "последние N записей"}},
       "required": []})
def t_audit_trail(args, ctx):
    from ..audit import AuditLog, summarize_changes
    base = Path(ctx.get("base_dir") or ".")
    audit_path = Path(args.get("audit_path") or base / "out" / "audit.jsonl")
    log = AuditLog(audit_path)
    action = args.get("action") or "list"
    if action == "verify":
        ok, count, message = log.verify()
        return {"ok": ok, "records": count, "message": message}
    if action == "show":
        record = log.find(_p(args, "hash"))
        return {"ok": bool(record), "record": record}
    records = log.history(entity=args.get("entity"), limit=args.get("limit"))
    return {"ok": True, "records": len(records), "trail": summarize_changes(records)}


@tool("materials_reconcile",
      "Сверка материалов: что решили закупить → что реально привезли (накладные/УПД) → что "
      "предъявили в КС-2. Находит замороженные деньги (недопоставка), приход без заявки, "
      "перерасход против решения, замену поставщика и предъявление в акте материалов, "
      "накладных на которые нет. Каждая сумма — с формулой и источником; нет цены — «не "
      "оценено», а не 0 ₽. Если в акте материалов построчно нет, так и говорит "
      "(`act_materials: unknown`) и не делает вывода «материалов в акте нет».",
      {"type": "object", "properties": {
          "decision_path": {"type": "string", "description": "decision.json из procure decide"},
          "receipts_paths": {"type": "array", "items": {"type": "string"},
                             "description": "накладные/УПД (csv, xlsx)"},
          "act_paths": {"type": "array", "items": {"type": "string"},
                        "description": "материалы КС-2 построчно (csv, xlsx), если есть"},
          "object_name": {"type": "string"},
          "out_dir": {"type": "string", "description": "куда сложить json/xlsx/md"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": ["decision_path"]})
def t_materials_reconcile(args, ctx):
    from ..materials import (export_materials_xlsx, materials_to_markdown, orders_from_decision,
                             read_act_materials, read_receipts, reconcile, write)
    base = Path(ctx.get("base_dir") or ".")
    decision = json.loads(Path(_p(args, "decision_path")).read_text(encoding="utf-8"))
    orders = orders_from_decision(decision)
    if not orders:
        raise ValueError("в решении нет ни одной позиции — сверять нечего")

    receipts, problems = [], []
    for path in args.get("receipts_paths") or []:
        loaded, issues = read_receipts(Path(path))
        receipts += loaded
        problems += issues
    act_materials, act_provided = [], False
    for path in args.get("act_paths") or []:
        loaded, issues = read_act_materials(Path(path))
        act_materials += loaded
        problems += issues
        act_provided = act_provided or bool(loaded)

    report = reconcile(orders, receipts, act_materials if act_provided else None,
                       object_name=args.get("object_name") or decision.get("object_name") or "",
                       request_no=decision.get("request_no") or "",
                       act_provided=act_provided)
    out_dir = Path(args.get("out_dir") or base / "out" / "materials")
    out_dir.mkdir(parents=True, exist_ok=True)
    write(out_dir / "materials.json", report)
    export_materials_xlsx(report, out_dir / "materials.xlsx")
    markdown = materials_to_markdown(report)
    (out_dir / "materials.md").write_text(markdown, encoding="utf-8")
    if (args.get("format") or "json") == "markdown":
        return {"ok": True, "format": "markdown", "markdown": markdown,
                "status": report["status"], "totals": report["totals"]}
    return {"ok": True, "report": report, "read_problems": problems, "markdown": markdown}


@tool("takeoff",
      "Объёмы из чертежей (BOQ от takeoff-инструмента) → ведомость объёмов с ценами из базы "
      "версий ФГИС ЦС. Уверенность строк не выбрасывается: HIGH идёт в итог, MEDIUM — в итог с "
      "пометкой «проверить», LOW и RATE_NOT_FOUND — в «не включено» с причиной. Нет цены или "
      "нет правила сопоставления — `no_price`, сумма null, а не 0 ₽. Шифр расценки ФЕР ставит "
      "человек при подтверждении; это ведомость объёмов с ценами, не смета.",
      {"type": "object", "properties": {
          "action": {"type": "string",
                     "enum": ["draft", "confirm", "pdf", "schedules"], "default": "draft",
                     "description": "pdf — прочитать том КР из PDF: факты с цитатами, объёмы "
                                    "расчётом по габаритам и толщинам; schedules — взять "
                                    "объёмы из ведомостей расхода стали и спецификаций, "
                                    "проверить арифметику и марки. Без выдуманных цифр"},
          "pdf_path": {"type": "string", "description": "PDF раздела КР (для action=pdf)"},
          "schedules_path": {"type": "string",
                             "description": "ведомости в JSON (для action=schedules)"},
          "boq_path": {"type": "string", "description": "выгрузка объёмов (csv/xlsx)"},
          "map_path": {"type": "string", "description": "словарь «элемент → код ресурса»"},
          "prices_dir": {"type": "string", "default": "data/prices"},
          "as_of": {"type": "string", "description": "цены на дату"},
          "object_name": {"type": "string"}, "section": {"type": "string", "default": "КР"},
          "out_dir": {"type": "string"},
          "draft_path": {"type": "string", "description": "для confirm: готовый draft.json"},
          "changes": {"type": "object",
                      "description": "для confirm: {элемент: {qty, norm, exclude, note}}"},
          "by": {"type": "string"}, "role": {"type": "string"}, "reason": {"type": "string"},
          "audit_path": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": ["action"]})
def t_takeoff(args, ctx):
    from ..takeoff import (build_draft, confirm, draft_to_markdown, export_draft_xlsx, load_map,
                           read_boq, write)
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("out_dir") or base / "out" / "takeoff")
    out_dir.mkdir(parents=True, exist_ok=True)
    prices_dir = Path(args.get("prices_dir") or base / "data" / "prices")
    audit = None
    if args.get("audit_path"):
        from ..audit import AuditLog
        audit = AuditLog(Path(args["audit_path"]))

    if (args.get("action") or "draft") == "pdf":
        from ..kr_pdf import (derive_boq, extract_facts, extract_levels, find_schedules,
                              kr_report, read_pdf_text)
        pdf_path = Path(_p(args, "pdf_path"))
        cache = out_dir / f"{pdf_path.stem}_pages.json"
        doc = read_pdf_text(pdf_path, cache=cache)
        schedules = find_schedules(doc)
        levels = extract_levels(doc)
        facts = extract_facts(doc)
        derived = derive_boq(facts, levels=levels)
        draft = build_draft(derived["lines"], mapping=load_map(args.get("map_path")),
                            prices_dir=prices_dir, as_of=args.get("as_of"),
                            object_name=args.get("object_name") or "",
                            section=args.get("section") or "КР1")
        write(out_dir / "draft.json", draft)
        export_draft_xlsx(draft, out_dir / "draft.xlsx")
        markdown = kr_report(doc, schedules, facts, derived,
                             object_name=args.get("object_name") or "", draft=draft)
        (out_dir / "kr_report.md").write_text(markdown, encoding="utf-8")
        return {"ok": True, "pages": doc["pages_total"], "schedules": schedules,
                "levels": levels["levels"], "facts": facts["facts"],
                "needs_input": derived["needs_input"], "draft": draft, "markdown": markdown}

    if (args.get("action") or "draft") == "schedules":
        from ..kr_schedules import (coverage as schedules_coverage, read_schedules,
                                    schedules_report, to_boq as schedules_to_boq,
                                    validate_schedules)
        doc = read_schedules(Path(_p(args, "schedules_path")))
        facts = {}
        if args.get("pdf_path") and Path(_p(args, "pdf_path")).exists():
            from ..kr_pdf import extract_facts, read_pdf_text
            pdf_doc = read_pdf_text(Path(_p(args, "pdf_path")),
                                    cache=out_dir / f"{Path(_p(args, 'pdf_path')).stem}_pages.json")
            facts = {"by_name": {f["name"]: f for f in extract_facts(pdf_doc)["facts"]}}
        validation = validate_schedules(doc, facts)
        boq = schedules_to_boq(doc, validation)
        cover = schedules_coverage(doc)
        draft = build_draft(boq["lines"], mapping=load_map(args.get("map_path")),
                            prices_dir=prices_dir, as_of=args.get("as_of"),
                            object_name=args.get("object_name") or doc.get("object") or "",
                            section=args.get("section") or "КР1")
        write(out_dir / "draft.json", draft)
        export_draft_xlsx(draft, out_dir / "draft.xlsx")
        markdown = schedules_report(doc, validation, boq, draft=draft, cover=cover)
        (out_dir / "kr_schedules_report.md").write_text(markdown, encoding="utf-8")
        return {"ok": True, "sheets": len(doc.get("sheets") or []),
                "needs_verification": bool(doc.get("needs_verification")),
                "findings": validation["findings"], "boq_totals": boq["totals_by_kind"],
                "missing": cover["missing"], "draft_totals": draft["totals"],
                "draft": draft, "markdown": markdown}

    if (args.get("action") or "draft") == "draft":
        boq, problems = read_boq(Path(_p(args, "boq_path")))
        if not boq:
            raise ValueError("в выгрузке объёмов нет ни одной строки")
        draft = build_draft(boq, mapping=load_map(args.get("map_path")),
                            prices_dir=prices_dir, as_of=args.get("as_of"),
                            object_name=args.get("object_name") or "",
                            section=args.get("section") or "КР")
        draft["read_problems"] = problems
    else:
        draft = json.loads(Path(_p(args, "draft_path")).read_text(encoding="utf-8"))
        draft = confirm(draft, args.get("changes") or {}, by=_p(args, "by"),
                        role=args.get("role") or "инженер ПТО",
                        reason=args.get("reason") or "", audit_log=audit)

    write(out_dir / "draft.json", draft)
    export_draft_xlsx(draft, out_dir / "draft.xlsx")
    markdown = draft_to_markdown(draft)
    (out_dir / "draft.md").write_text(markdown, encoding="utf-8")
    if (args.get("format") or "json") == "markdown":
        return {"ok": True, "format": "markdown", "markdown": markdown,
                "totals": draft["totals"]}
    return {"ok": True, "draft": draft, "markdown": markdown}


@tool("tk_draft",
      "Черновик технологической карты (ТК) или проекта производства работ (ППР). Заполняется "
      "только то, что есть в матрице требований (вид работ, скрываемость, комплект ИД, "
      "протоколы), в нормативной базе (нормы — со сверкой и цитатой) и в переданной смете "
      "(позиции с пометкой «проверить»). Состав операций с допусками, машины и механизмы, "
      "охрана труда и калькуляция трудозатрат не выдумываются: возвращаются как `needs_input` "
      "с адресатом запроса. Норма без сверки помечается «не проверено». Документ всегда "
      "черновик: подтверждение принимается только с ФИО и ролью, и после него это «черновик "
      "проверен инженером», а не утверждённая ТК.",
      {"type": "object", "properties": {
          "action": {"type": "string", "enum": ["draft", "ppr", "confirm"], "default": "draft",
                     "description": "draft — ТК по виду работ; ppr — ППР по объекту; "
                                    "confirm — отметить проверку человеком"},
          "work_type": {"type": "string",
                        "description": "id вида работ из матрицы (earthworks, electrical, …)"},
          "text": {"type": "string", "description": "текст работы: вид работ определит классификатор"},
          "object_name": {"type": "string"},
          "estimate_path": {"type": "string",
                            "description": "смета (xlsx): позиции пойдут в раздел «Материалы»"},
          "norms_dir": {"type": "string", "description": "каталог нормативной базы"},
          "out_dir": {"type": "string"},
          "draft_path": {"type": "string",
                         "description": "для confirm: JSON черновика (tk_<вид работ>.json)"},
          "filled": {"type": "object",
                     "description": "для confirm: {ключ раздела: [что вписал инженер]}"},
          "by": {"type": "string", "description": "ФИО проверившего (обязательно для confirm)"},
          "role": {"type": "string", "description": "должность (обязательно для confirm)"},
          "reason": {"type": "string"},
          "audit_path": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": ["action"]})
def t_tk_draft(args, ctx):
    from ..tk import (build_ppr_draft, build_tk_draft, confirm as confirm_tk, to_markdown,
                      write)
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("out_dir") or base / "out" / "tk")
    action = args.get("action") or "draft"
    audit = None
    if args.get("audit_path"):
        from ..audit import AuditLog
        audit = AuditLog(Path(args["audit_path"]))

    positions = None
    if args.get("estimate_path"):
        from ..parsers import read_estimate_ex
        items, problems = read_estimate_ex(Path(_p(args, "estimate_path")))
        positions = [{"name": i.name, "qty": i.qty, "unit": i.unit,
                      "source": f"смета {Path(_p(args, 'estimate_path')).name}"} for i in items]
        if problems:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "estimate_problems.json").write_text(
                json.dumps(problems, ensure_ascii=False, indent=1), encoding="utf-8")

    if action == "confirm":
        draft = json.loads(Path(_p(args, "draft_path")).read_text(encoding="utf-8"))
        draft = confirm_tk(draft, by=args.get("by") or "", role=args.get("role") or "",
                           filled=args.get("filled") or {}, reason=args.get("reason") or "",
                           audit_log=audit)
        if draft.get("confirm_problems"):
            return {"ok": False, "error": "подтверждение не принято",
                    "problems": draft["confirm_problems"]}
    elif action == "ppr":
        draft = build_ppr_draft(object_name=args.get("object_name") or "", positions=positions)
    else:
        draft = build_tk_draft(work_type_id=args.get("work_type") or None,
                               work_text=args.get("text") or None,
                               object_name=args.get("object_name") or "",
                               positions=positions, norms_dir=args.get("norms_dir") or None)

    files = write(draft, out_dir,
                  **({"stem": Path(_p(args, "draft_path")).stem} if action == "confirm" else {}))
    payload = {"ok": True, "kind": draft["kind"], "status": draft["status"],
               "work_type": draft.get("work_type"), "totals": draft["totals"],
               "needs_input": draft["needs_input"], "unverified_norms":
                   [n for n in draft.get("norms") or [] if not n["verified"]],
               "files": files, "disclaimer": draft["disclaimer"]}
    if (args.get("format") or "json") == "markdown":
        payload["markdown"] = to_markdown(draft)
    return payload


@tool("tk_registry",
      "Реестр технологических карт объекта: объект → ТК/ППР → статус → кто проверил. Читает "
      "готовые черновики (JSON) и собранный аудит, а не спрашивает заново. Статус «проверен "
      "инженером» ставится только при ФИО и роли и только пока содержание совпадает с "
      "проверенным (отпечаток): если после проверки правили разделы — «проверка "
      "недействительна». Черновик без проверки человека — «не проверен», а не «готово». "
      "Реестр ничего не утверждает и не даёт права применять карты на объекте.",
      {"type": "object", "properties": {
          "paths": {"type": "array", "items": {"type": "string"},
                    "description": "каталоги или JSON черновиков (по умолчанию out/tk)"},
          "out_dir": {"type": "string"},
          "strict": {"type": "boolean", "default": False,
                     "description": "true — считать провалом карты без проверки человеком"},
          "audit_path": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": []})
def t_tk_registry(args, ctx):
    from ..tk_registry import (build as build_registry, load_drafts, to_markdown, verify,
                               write as write_registry)
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("out_dir") or base / "out" / "tk")
    paths = [Path(p) if Path(p).is_absolute() else base / p for p in (args.get("paths") or [])]
    drafts, problems = load_drafts(paths or [out_dir])
    registry = build_registry(drafts, problems=problems)
    files = write_registry(registry, out_dir)
    integrity = verify(registry)
    totals = registry["totals"]

    payload = {
        "ok": not (problems or integrity),
        "totals": totals,
        "objects": [{"object_name": slot["object_name"], "state": slot["state"],
                     "totals": slot["totals"], "waiting_for": slot["waiting_for"],
                     "drafts": [{k: e[k] for k in ("kind", "work_type", "work_type_name",
                                                   "status", "by", "role", "confirmed_at",
                                                   "check_valid", "needs_input", "file")}
                                for e in slot["drafts"]]}
                    for slot in registry["objects"]],
        "changed_after_confirm": registry["changed_after_confirm"],
        "not_checked": registry["not_checked"],
        "problems": problems,
        "integrity_problems": integrity,
        "files": files,
        "disclaimer": registry["disclaimer"],
    }
    if args.get("strict") and registry["not_checked"]:
        payload["ok"] = False
        payload["strict_failed"] = True
    if (args.get("format") or "json") == "markdown":
        payload["markdown"] = to_markdown(registry)
    if args.get("audit_path"):
        from ..audit import AuditLog
        AuditLog(Path(args["audit_path"])).append(
            "agent:tk", "agent", "tk.registry_built", "tk:registry",
            {"objects": totals["objects"], "drafts": totals["drafts"],
             "confirmed": totals["confirmed"],
             "changed_after_confirm": totals["changed_after_confirm"]})
    return payload


@tool("cumulative_ledger",
      "Накопительная ведомость выполненных работ: по позициям (код, наименование, единица) "
      "количество и сумма по периодам, итог нарастающим итогом с начала работ и остаток к "
      "смете. Проверяет то, что обычно прячется: сумма акта против суммы позиций, дубль акта, "
      "одна позиция в двух актах одного периода, разные единицы под одним кодом, отрицательные "
      "строки (сторно), позиции вне сметы и превышение сметы. Числа разбираются строго: `nan` "
      "и мусор не становятся нулём, а попадают в проверки. Ведомость показывает предъявленное "
      "и не подтверждает объёмы.",
      {"type": "object", "properties": {
          "acts_path": {"type": "string", "description": "реестр актов объекта (acts.json)"},
          "estimate_path": {"type": "string", "description": "смета (xlsx): остаток и цены"},
          "out_dir": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": ["acts_path"]})
def t_cumulative_ledger(args, ctx):
    from ..cumulative import build_ledger, load_acts, load_estimate, to_markdown, write
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("out_dir") or base / "out" / "cumulative")
    acts_data, problems = load_acts(Path(_p(args, "acts_path")))
    if problems:
        return {"ok": False, "error": "акты не прочитаны", "problems": problems}
    estimate = load_estimate(Path(_p(args, "estimate_path"))) if args.get("estimate_path") else {}
    ledger = build_ledger(acts_data, estimate=estimate)
    files = write(ledger, out_dir)
    payload = {"ok": not ledger["totals"]["errors"], "object": ledger["object"],
               "contract": ledger["contract"], "periods": ledger["periods"],
               "totals": ledger["totals"], "rows": ledger["rows"], "acts": ledger["acts"],
               "checks": ledger["checks"], "files": files,
               "disclaimer": ledger["disclaimer"]}
    if (args.get("format") or "json") == "markdown":
        payload["markdown"] = to_markdown(ledger)
    return payload


@tool("atn_draft",
      "Акт на вновь возникающие и дополнительные работы (АТН): собирает позиции, у которых есть "
      "ОСНОВАНИЕ (лист РД, исполнительная схема, письмо, задание заказчика, протокол, расчёт) и "
      "цена. Позиция без основания в акт не попадает — она уходит в `needs_basis` со своей "
      "суммой; без цены — в `no_price` (сумма не считается, а не ноль рублей); количество не "
      "число или ≤ 0 — в `rejected`. Повтор позиции в одном акте не считается дважды. "
      "`action=confirm` принимает только ФИО и роль человека; статус после этого «проверен "
      "инженером (не подписан заказчиком)» — акт подписывают заказчик и подрядчик.",
      {"type": "object", "properties": {
          "action": {"type": "string", "enum": ["draft", "confirm"], "default": "draft"},
          "items": {"type": "array", "items": {"type": "object"},
                    "description": "позиции: {code, name, unit, qty, price, "
                                   "basis:{kind, ref, date}}"},
          "items_path": {"type": "string", "description": "JSON позиций вместо items"},
          "acts_path": {"type": "string", "description": "реестр актов: что уже предъявлено"},
          "estimate_path": {"type": "string", "description": "смета: цена и остаток по коду"},
          "number": {"type": "string"}, "object_name": {"type": "string"},
          "contract": {"type": "string"}, "customer": {"type": "string"},
          "contractor": {"type": "string"}, "reason": {"type": "string"},
          "atn_path": {"type": "string", "description": "для confirm: JSON черновика АТН"},
          "by": {"type": "string", "description": "ФИО проверившего (для confirm)"},
          "role": {"type": "string", "description": "должность (для confirm)"},
          "out_dir": {"type": "string"}, "audit_path": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": []})
def t_atn_draft(args, ctx):
    from ..atn import build_atn, confirm as confirm_atn, to_markdown, write
    from ..cumulative import build_ledger, load_acts, load_estimate
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("out_dir") or base / "out" / "atn")
    audit = None
    if args.get("audit_path"):
        from ..audit import AuditLog
        audit = AuditLog(Path(_p(args, "audit_path")))
    action = args.get("action") or "draft"

    if action == "confirm":
        path = Path(_p(args, "atn_path"))
        atn = json.loads(path.read_text(encoding="utf-8"))
        atn = confirm_atn(atn, by=args.get("by") or "", role=args.get("role") or "",
                          reason=args.get("reason") or "", audit_log=audit)
        if atn.get("confirm_problems"):
            return {"ok": False, "error": "подтверждение не принято",
                    "problems": atn["confirm_problems"]}
        files = write(atn, out_dir, stem=path.stem.replace("atn_", ""))
        return {"ok": True, "status": atn["status"], "totals": atn["totals"],
                "confirmed_by": atn["confirmed_by"], "files": files,
                "disclaimer": atn["disclaimer"]}

    items = args.get("items") or []
    if not items and args.get("items_path"):
        raw = json.loads(Path(_p(args, "items_path")).read_text(encoding="utf-8"))
        items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return {"ok": False, "error": "позиции не прочитаны", "problems": ["ожидался список"]}

    ledger = {}
    problems: list = []
    if args.get("acts_path"):
        acts_data, problems = load_acts(Path(_p(args, "acts_path")))
        if acts_data:
            ledger = build_ledger(
                acts_data,
                estimate=load_estimate(Path(_p(args, "estimate_path")))
                if args.get("estimate_path") else {})
    estimate = load_estimate(Path(_p(args, "estimate_path"))) if args.get("estimate_path") else {}
    atn = build_atn(items, number=args.get("number") or "",
                    object_name=args.get("object_name") or "", contract=args.get("contract") or "",
                    customer=args.get("customer") or "", contractor=args.get("contractor") or "",
                    ledger=ledger, estimate=estimate, reason=args.get("reason") or "")
    files = write(atn, out_dir)
    if audit is not None:
        audit.append("agent:atn", "agent", "atn.draft_built",
                     f"atn:{atn.get('number') or 'б/н'}",
                     {"to_claim_rub": atn["totals"]["to_claim_rub"],
                      "included": atn["totals"]["included"],
                      "needs_basis": atn["totals"]["needs_basis"]})
    payload = {"ok": True, "status": atn["status"], "number": atn["number"],
               "object_name": atn["object_name"], "totals": atn["totals"],
               "positions": atn["positions"], "needs_basis": atn["needs_basis"],
               "no_price": atn["no_price"], "rejected": atn["rejected"],
               "problems": problems, "files": files, "disclaimer": atn["disclaimer"]}
    if (args.get("format") or "json") == "markdown":
        payload["markdown"] = to_markdown(atn)
    return payload


@tool("consumption_report",
      "Материалы в цифрах: потребность по нормам расхода (объём работ × норма), списание по "
      "актам против нормы, лимит закупки против факта, давальческие материалы. Норма без "
      "документа, пункта и редакции НЕ применяется; норма с источником сверяется с нормативной "
      "базой, и если документа в базе нет — расчёт идёт как «справочный» и в подтверждённый итог "
      "не входит, пока норму не примет человек (`confirm_norms` + ФИО и роль). Перерасход без "
      "причины — ошибка, списание без нормы не проверяется вообще, нет цены — лимит в деньгах не "
      "считается (не ноль). Давальческий материал, попавший и в собственные закупки, — двойной "
      "счёт. Закупку и списание решает человек: агент считает и показывает расхождения.",
      {"type": "object", "properties": {
          "works_path": {"type": "string", "description": "ведомость объёмов работ (JSON)"},
          "norms_path": {"type": "string", "description": "нормы расхода (JSON, с источником)"},
          "writeoffs_path": {"type": "string", "description": "списание по актам (xlsx, csv)"},
          "purchases_path": {"type": "string", "description": "закупки (xlsx, csv)"},
          "daval_path": {"type": "string", "description": "давальческие материалы (xlsx, csv)"},
          "object_name": {"type": "string"},
          "norms_dir": {"type": "string", "description": "каталог нормативной базы для сверки"},
          "confirm_norms": {"type": "boolean", "default": False,
                            "description": "true — нормы приняты человеком (нужны by и role)"},
          "by": {"type": "string", "description": "ФИО того, кто принял нормы"},
          "role": {"type": "string", "description": "должность"},
          "out_dir": {"type": "string"}, "audit_path": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": ["works_path", "norms_path"]})
def t_consumption_report(args, ctx):
    from ..consumption import build_report, load_json, load_table, to_markdown, write
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("out_dir") or base / "out" / "consumption")

    works, problems = load_json(Path(_p(args, "works_path")), "works")
    norms, norm_problems = load_json(Path(_p(args, "norms_path")), "norms")
    problems += norm_problems
    if problems:
        return {"ok": False, "error": "входные данные не прочитаны", "problems": problems}

    writeoffs, p1 = (load_table(Path(_p(args, "writeoffs_path")), "writeoff")
                     if args.get("writeoffs_path") else ([], []))
    purchases, p2 = (load_table(Path(_p(args, "purchases_path")), "purchase")
                     if args.get("purchases_path") else ([], []))
    daval, p3 = (load_table(Path(_p(args, "daval_path")), "daval")
                 if args.get("daval_path") else ([], []))
    problems += p1 + p2 + p3

    confirm = bool(args.get("confirm_norms"))
    by, role = args.get("by") or "", args.get("role") or ""
    if confirm and not (by.strip() and role.strip()):
        return {"ok": False, "error": "принятие норм без человека не принимается",
                "problems": ["confirm_norms требует ФИО (by) и роли (role): норму принимает "
                             "человек, а не агент"]}

    report = build_report(works, norms, writeoffs=writeoffs, purchases=purchases, daval=daval,
                          object_name=args.get("object_name") or "",
                          folder=args.get("norms_dir") or None,
                          confirmed_by=by, role=role, confirm_norms=confirm)
    files = write(report, out_dir)
    if args.get("audit_path"):
        from ..audit import AuditLog
        AuditLog(Path(args["audit_path"])).append(
            "agent:materials", "agent", "materials.consumption_built",
            f"object:{report['object_name'] or 'без объекта'}",
            {"need_confirmed_rub": report["totals"]["need_confirmed_rub"],
             "over_norms": report["totals"]["over_norms"],
             "over_limit": report["totals"]["over_limit"],
             "norms_confirmed_by": (report["norm_confirmation"] or {}).get("by")})
    payload = {"ok": not report["totals"]["errors"], "object_name": report["object_name"],
               "norms": report["norms"], "totals": report["totals"],
               "need": report["need"], "writeoff": report["writeoff"],
               "limits": report["limits"], "daval": report["daval"],
               "checks": report["checks"], "problems": problems,
               "norm_confirmation": report["norm_confirmation"], "files": files,
               "disclaimer": report["disclaimer"]}
    if (args.get("format") or "json") == "markdown":
        payload["markdown"] = to_markdown(report)
    return payload


@tool("checklist",
      "Чек-лист выполненных работ: пункты контроля с источником (норма + пункт + редакция "
      "или чек-лист организации), результат «соответствует / не соответствует / не проверено», "
      "примечание и подпись человека. Пункт без источника не создаётся. `action=sign` "
      "принимается только при ФИО и роли, только когда все пункты проверены и у каждого "
      "несоответствия есть описание: непроверенное не подписывается. Возвращает `can_close` — "
      "можно ли предъявлять работы, с причинами; при несоответствиях работы не приняты. "
      "Правка результатов после подписи аннулирует подпись (отпечаток содержания).",
      {"type": "object", "properties": {
          "action": {"type": "string", "enum": ["build", "fill", "sign"], "default": "build"},
          "work_type": {"type": "string", "description": "id вида работ из матрицы"},
          "object_name": {"type": "string"}, "number": {"type": "string"},
          "act_ref": {"type": "string", "description": "акт КС-2, к которому относится чек-лист"},
          "items": {"type": "array", "items": {"type": "object"},
                    "description": "пункты организации: {requirement, control, "
                                   "source:{doc, point, edition}}"},
          "items_path": {"type": "string", "description": "JSON пунктов вместо items"},
          "checklist_path": {"type": "string", "description": "для fill/sign: JSON чек-листа"},
          "results": {"type": "array", "items": {"type": "object"},
                      "description": "для fill: [{no, result, note, measured}]"},
          "results_path": {"type": "string"},
          "by": {"type": "string", "description": "ФИО (для fill — заполнил, для sign — подписал)"},
          "role": {"type": "string", "description": "должность (обязательна для sign)"},
          "reason": {"type": "string"},
          "out_dir": {"type": "string"}, "audit_path": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": []})
def t_checklist(args, ctx):
    from ..checklist import build_checklist, fill, sign, to_markdown, write
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("out_dir") or base / "out" / "checklists")
    audit = None
    if args.get("audit_path"):
        from ..audit import AuditLog
        audit = AuditLog(Path(args["audit_path"]))
    action = args.get("action") or "build"

    if action == "build":
        items = args.get("items") or []
        if not items and args.get("items_path"):
            raw = json.loads(Path(_p(args, "items_path")).read_text(encoding="utf-8"))
            items = raw.get("items") if isinstance(raw, dict) else raw
        checklist = build_checklist(work_type_id=args.get("work_type") or "",
                                    object_name=args.get("object_name") or "",
                                    number=args.get("number") or "",
                                    act_ref=args.get("act_ref") or "", items=items or None)
        files = write(checklist, out_dir)
        payload = {"ok": checklist["totals"]["rejected"] == 0, "status": checklist["status"],
                   "work_type": checklist.get("work_type"), "totals": checklist["totals"],
                   "items": checklist["items"], "rejected": checklist["rejected"],
                   "checks": checklist["checks"], "can_close": checklist["can_close"],
                   "files": files, "disclaimer": checklist["disclaimer"]}
        if (args.get("format") or "json") == "markdown":
            payload["markdown"] = to_markdown(checklist)
        return payload

    path = Path(_p(args, "checklist_path"))
    checklist = json.loads(path.read_text(encoding="utf-8"))

    if action == "fill":
        results = args.get("results") or []
        if not results and args.get("results_path"):
            raw = json.loads(Path(_p(args, "results_path")).read_text(encoding="utf-8"))
            results = raw.get("results") if isinstance(raw, dict) else raw
        checklist = fill(checklist, results or [], by=args.get("by") or "")
        files = write(checklist, out_dir, stem=path.stem.replace("checklist_", ""))
        payload = {"ok": not (checklist.get("problems") or
                              [c for c in checklist.get("checks") or [] if c["level"] == "error"]),
                   "status": checklist["status"], "totals": checklist["totals"],
                   "items": checklist["items"], "problems": checklist.get("problems") or [],
                   "checks": checklist["checks"], "can_close": checklist["can_close"],
                   "files": files}
        if (args.get("format") or "json") == "markdown":
            payload["markdown"] = to_markdown(checklist)
        return payload

    if action == "sign":
        checklist = sign(checklist, by=args.get("by") or "", role=args.get("role") or "",
                         reason=args.get("reason") or "", audit_log=audit)
        if checklist.get("sign_problems"):
            return {"ok": False, "error": "подпись не принята",
                    "problems": checklist["sign_problems"],
                    "status": checklist.get("status")}
        files = write(checklist, out_dir, stem=path.stem.replace("checklist_", ""))
        return {"ok": True, "status": checklist["status"], "totals": checklist["totals"],
                "confirmed_by": checklist["confirmed_by"], "can_close": checklist["can_close"],
                "files": files, "disclaimer": checklist["disclaimer"]}

    return {"ok": False, "error": f"неизвестное действие: {action}"}


@tool("journals",
      "Два журнала комплекта ИД: общий журнал работ и журнал верификации закупленной продукции "
      "(входной контроль). `action=work`: записи по сменам (дата, смена, работы, объём, "
      "исполнитель, погода) — вид работ определяется классификатором матрицы, пустые записи "
      "отклоняются, даты проверяются на хронологию, для земляных/бетонных/кровельных работ "
      "требуется температура воздуха, объёмы сверяются с актом КС-2. `action=verification`: "
      "партия → документ качества → решение → кто проверил; решение «допущен» невозможно без "
      "документа качества и по документу с истёкшим на дату проверки сроком; материал, "
      "списанный без входного контроля, показывается отдельно. `action=sign` — подпись человека "
      "(ФИО и роль), при ошибках в журнале подпись не принимается; журнал закрывает ИТР.",
      {"type": "object", "properties": {
          "action": {"type": "string", "enum": ["work", "verification", "sign"],
                     "default": "work"},
          "entries": {"type": "array", "items": {"type": "object"},
                      "description": "для work: [{date, shift, work, qty, unit, executor, zone, "
                                     "weather{temp_c, precipitation}, supervision}]"},
          "entries_path": {"type": "string"},
          "records": {"type": "array", "items": {"type": "object"},
                      "description": "для verification: [{date, material, unit, qty, batch, "
                                     "quality_doc{kind, number, valid_until}, decision, "
                                     "inspected_by, role}]"},
          "records_path": {"type": "string"},
          "writeoffs_path": {"type": "string",
                             "description": "для verification: списание (xlsx, csv)"},
          "acts_path": {"type": "string", "description": "для work: реестр актов КС-2"},
          "journal_path": {"type": "string", "description": "для sign: JSON журнала"},
          "object_name": {"type": "string"}, "number": {"type": "string"},
          "by": {"type": "string", "description": "ФИО (для sign)"},
          "role": {"type": "string", "description": "должность (для sign)"},
          "reason": {"type": "string"},
          "out_dir": {"type": "string"}, "audit_path": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": []})
def t_journals(args, ctx):
    from ..journals import (build_verification_journal, build_work_journal, load_json, sign,
                            to_markdown, write)
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("out_dir") or base / "out" / "journals")
    audit = None
    if args.get("audit_path"):
        from ..audit import AuditLog
        audit = AuditLog(Path(args["audit_path"]))
    action = args.get("action") or "work"

    if action == "sign":
        path = Path(_p(args, "journal_path"))
        journal = json.loads(path.read_text(encoding="utf-8"))
        journal = sign(journal, by=args.get("by") or "", role=args.get("role") or "",
                       reason=args.get("reason") or "", audit_log=audit)
        if journal.get("sign_problems"):
            return {"ok": False, "error": "подпись не принята",
                    "problems": journal["sign_problems"], "can_close": journal.get("can_close")}
        files = write(journal, out_dir, stem=path.stem)
        return {"ok": True, "kind": journal["schema"], "status": journal["status"],
                "totals": journal["totals"], "confirmed_by": journal["confirmed_by"],
                "can_close": journal["can_close"], "files": files,
                "disclaimer": journal["disclaimer"]}

    if action == "work":
        entries = args.get("entries") or []
        problems: list = []
        if not entries and args.get("entries_path"):
            entries, problems = load_json(Path(_p(args, "entries_path")), "entries")
        acts = None
        if args.get("acts_path"):
            from ..cumulative import load_acts
            acts_data, act_problems = load_acts(Path(_p(args, "acts_path")))
            problems += act_problems
            acts = acts_data or None
        journal = build_work_journal(entries, object_name=args.get("object_name") or "",
                                     number=args.get("number") or "", acts=acts)
    else:
        records = args.get("records") or []
        problems = []
        if not records and args.get("records_path"):
            records, problems = load_json(Path(_p(args, "records_path")), "records")
        writeoffs = []
        if args.get("writeoffs_path"):
            from ..consumption import load_table
            writeoffs, more = load_table(Path(_p(args, "writeoffs_path")), "writeoff")
            problems += more
        journal = build_verification_journal(records, object_name=args.get("object_name") or "",
                                             number=args.get("number") or "",
                                             writeoffs=writeoffs)

    files = write(journal, out_dir)
    if audit is not None:
        audit.append("agent:journal", "agent", "journal.built", f"journal:{journal.get('number')}",
                     {"kind": journal["schema"], "records": journal["totals"].get("entries")
                      or journal["totals"].get("records")})
    payload = {"ok": journal["totals"]["errors"] == 0, "kind": journal["schema"],
               "status": journal["status"], "totals": journal["totals"],
               "checks": journal["checks"], "can_close": journal["can_close"],
               "problems": problems, "files": files, "disclaimer": journal["disclaimer"]}
    payload["entries" if action == "work" else "records"] = journal.get(
        "entries" if action == "work" else "records")
    if action == "verification":
        payload["unverified_writeoffs"] = journal["unverified_writeoffs"]
    if action == "work":
        payload["by_work_type"] = journal["by_work_type"]
    if (args.get("format") or "json") == "markdown":
        payload["markdown"] = to_markdown(journal)
    return payload


@tool("quality_docs",
      "Документы качества на материалы и оборудование: реестр с привязкой к партиям. "
      "Документ проверяется по трём вещам: тот ли материал (наименование, марка, фракция), та ли "
      "партия, действовал ли документ на дату применения. Тип документа вне рабочего перечня "
      "(товарный чек, счёт) документом качества не считается; копия без отметки «копия верна» в "
      "комплект не идёт. Состав требуемых документов берётся из файла требований (проект, "
      "договор, ТУ) — агент его не выдумывает: нет требования, материал показывается как "
      "«требование не заявлено». Стык с журналом входного контроля: допуск по документу, "
      "которого нет в реестре, — ошибка. `action=sign` — подпись человека с ФИО и ролью, при "
      "ошибках отказ.",
      {"type": "object", "properties": {
          "action": {"type": "string", "enum": ["build", "sign"], "default": "build"},
          "documents": {"type": "array", "items": {"type": "object"},
                        "description": "[{kind, number, issue_date, valid_until, material, "
                                       "batch, manufacturer, standard, pages, file, "
                                       "attested_by{by, role, at}}]"},
          "documents_path": {"type": "string"},
          "requirements": {"type": "array", "items": {"type": "object"},
                           "description": "[{material, required: [типы документов], basis}]"},
          "requirements_path": {"type": "string"},
          "usage": {"type": "array", "items": {"type": "object"},
                    "description": "применение партий: [{material, batch, qty, unit, "
                                   "applied_from, applied_to}]"},
          "usage_path": {"type": "string"},
          "verification": {"type": "array", "items": {"type": "object"},
                           "description": "записи журнала входного контроля (круг 7)"},
          "verification_path": {"type": "string"},
          "registry_path": {"type": "string", "description": "для sign: JSON реестра"},
          "object_name": {"type": "string"}, "number": {"type": "string"},
          "by": {"type": "string", "description": "ФИО (для sign)"},
          "role": {"type": "string", "description": "должность (для sign)"},
          "reason": {"type": "string"},
          "out_dir": {"type": "string"}, "audit_path": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": []})
def t_quality_docs(args, ctx):
    from ..quality_docs import build_registry, load_json, sign, to_markdown, write
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("out_dir") or base / "out" / "quality")
    audit = None
    if args.get("audit_path"):
        from ..audit import AuditLog
        audit = AuditLog(Path(args["audit_path"]))

    if (args.get("action") or "build") == "sign":
        path = Path(_p(args, "registry_path"))
        registry = json.loads(path.read_text(encoding="utf-8"))
        registry = sign(registry, by=args.get("by") or "", role=args.get("role") or "",
                        reason=args.get("reason") or "", audit_log=audit)
        if registry.get("sign_problems"):
            return {"ok": False, "error": "подпись не принята",
                    "problems": registry["sign_problems"],
                    "can_transfer": registry.get("can_transfer")}
        files = write(registry, out_dir, stem=path.stem)
        return {"ok": True, "kind": registry["schema"], "status": registry["status"],
                "totals": registry["totals"], "confirmed_by": registry["confirmed_by"],
                "can_transfer": registry["can_transfer"], "files": files,
                "disclaimer": registry["disclaimer"]}

    problems: list = []
    documents = args.get("documents") or []
    if not documents and args.get("documents_path"):
        documents, problems = load_json(Path(_p(args, "documents_path")), "documents")
    requirements = args.get("requirements") or []
    if not requirements and args.get("requirements_path"):
        more, extra = load_json(Path(_p(args, "requirements_path")), "requirements")
        requirements, problems = more, problems + extra
    usage = args.get("usage") or []
    if not usage and args.get("usage_path"):
        more, extra = load_json(Path(_p(args, "usage_path")), "usage")
        usage, problems = more, problems + extra
    verification = args.get("verification") or []
    if not verification and args.get("verification_path"):
        more, extra = load_json(Path(_p(args, "verification_path")), "records")
        verification, problems = more, problems + extra

    registry = build_registry(documents, object_name=args.get("object_name") or "",
                              number=args.get("number") or "", requirements=requirements,
                              usage=usage, verification=verification)
    files = write(registry, out_dir)
    if audit is not None:
        audit.append("agent:quality", "agent", "quality.built",
                     f"quality:{registry.get('number') or 'б/н'}",
                     {"documents": registry["totals"]["documents"],
                      "materials": registry["totals"]["materials"]})
    payload = {"ok": registry["totals"]["errors"] == 0, "kind": registry["schema"],
               "status": registry["status"], "totals": registry["totals"],
               "documents": registry["documents"], "materials": registry["materials"],
               "rejected": registry["rejected"], "checks": registry["checks"],
               "can_transfer": registry["can_transfer"], "problems": problems,
               "files": files, "disclaimer": registry["disclaimer"]}
    if (args.get("format") or "json") == "markdown":
        payload["markdown"] = to_markdown(registry)
    return payload


@tool("docflow",
      "Движение документации. `action=rd`: реестр выдачи рабочей документации — какая редакция "
      "передана, кому, когда, со штампом ли «В производство работ», есть ли подпись получившего; "
      "передача заменённой редакции и работа по документу, которого нет в выдаче, — ошибки. "
      "`action=submittals`: согласование ИД — комплект, замечания со сроками, устранение, "
      "повторная передача, подпись; подпись при открытых замечаниях и подпись без ФИО и роли "
      "недействительны. `action=sign` — подпись реестра человеком с ФИО и ролью, при ошибках отказ.",
      {"type": "object", "properties": {
          "action": {"type": "string", "enum": ["rd", "submittals", "sign"], "default": "rd"},
          "transmittals": {"type": "array", "items": {"type": "object"},
                           "description": "[{doc, title, revision, date, recipient, copies, "
                                          "stamp, receipt{by, at}}]"},
          "transmittals_path": {"type": "string"},
          "revisions": {"type": "array", "items": {"type": "object"},
                        "description": "реестр редакций: [{doc, revision, current}]"},
          "revisions_path": {"type": "string", "description": "реестр редакций РД"},
          "works": {"type": "array", "items": {"type": "object"},
                    "description": "работы со ссылкой на РД: [{work, rd}]"},
          "works_path": {"type": "string", "description": "работы со ссылкой на РД"},
          "submittals": {"type": "array", "items": {"type": "object"},
                         "description": "[{title, act_ref, sent_at, recipient, receipt{by}, "
                                        "remarks[{text, due, fixed_at}], signature{by, role, "
                                        "decision, at}, resubmitted_at}]"},
          "submittals_path": {"type": "string"},
          "registry_path": {"type": "string", "description": "для sign: JSON реестра"},
          "object_name": {"type": "string"}, "number": {"type": "string"},
          "by": {"type": "string"}, "role": {"type": "string"},
          "decision": {"type": "string"}, "reason": {"type": "string"},
          "out_dir": {"type": "string"}, "audit_path": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": []})
def t_docflow(args, ctx):
    from ..docflow import (build_rd_registry, build_submittal_registry, load_list, sign,
                           to_markdown, write)
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("out_dir") or base / "out" / "docflow")
    audit = None
    if args.get("audit_path"):
        from ..audit import AuditLog
        audit = AuditLog(Path(args["audit_path"]))
    action = args.get("action") or "rd"

    if action == "sign":
        path = Path(_p(args, "registry_path"))
        document = json.loads(path.read_text(encoding="utf-8"))
        document = sign(document, by=args.get("by") or "", role=args.get("role") or "",
                        decision=args.get("decision") or "", reason=args.get("reason") or "",
                        audit_log=audit)
        if document.get("sign_problems"):
            return {"ok": False, "error": "подпись не принята",
                    "problems": document["sign_problems"]}
        files = write(document, out_dir, stem=path.stem)
        return {"ok": True, "kind": document["schema"], "status": document["status"],
                "totals": document["totals"], "confirmed_by": document["confirmed_by"],
                "can_work": document.get("can_work"), "can_close": document.get("can_close"),
                "files": files, "disclaimer": document["disclaimer"]}

    problems: list = []
    if action == "rd":
        transmittals = args.get("transmittals") or []
        if not transmittals and args.get("transmittals_path"):
            transmittals, problems = load_list(Path(_p(args, "transmittals_path")), "transmittals")
        revisions: list = args.get("revisions") or []
        if not revisions and args.get("revisions_path"):
            revisions, more = load_list(Path(_p(args, "revisions_path")), "revisions")
            problems += more
        works: list = args.get("works") or []
        if not works and args.get("works_path"):
            works, more = load_list(Path(_p(args, "works_path")), "works")
            problems += more
        document = build_rd_registry(transmittals, revisions=revisions, works=works,
                                     object_name=args.get("object_name") or "",
                                     number=args.get("number") or "")
    else:
        submittals = args.get("submittals") or []
        if not submittals and args.get("submittals_path"):
            submittals, problems = load_list(Path(_p(args, "submittals_path")), "submittals")
        document = build_submittal_registry(submittals, object_name=args.get("object_name") or "",
                                            number=args.get("number") or "")

    files = write(document, out_dir)
    if audit is not None:
        audit.append("agent:docflow", "agent", "docflow.built",
                     f"{document['schema']}:{document.get('number') or 'б/н'}",
                     {"errors": document["totals"]["errors"]})
    payload = {"ok": document["totals"]["errors"] == 0, "kind": document["schema"],
               "status": document["status"], "totals": document["totals"],
               "checks": document["checks"], "rejected": document["rejected"],
               "can_work": document.get("can_work"), "can_close": document.get("can_close"),
               "problems": problems, "files": files, "disclaimer": document["disclaimer"]}
    payload["transmittals" if action == "rd" else "submittals"] = document.get(
        "transmittals" if action == "rd" else "submittals")
    payload["documents"] = document.get("documents")
    if (args.get("format") or "json") == "markdown":
        payload["markdown"] = to_markdown(document)
    return payload


@tool("correspondence",
      "Внешние участники и переписка. `action=letters`: письма и запросы — адресат, тема, "
      "основание (пункт договора, норма, номер письма), ФИО и роль подписавшего, срок ответа; "
      "письмо без основания не предъявляется, просроченный ответ — ошибка. `action=involvement`: "
      "лаборатории и геодезисты — аттестат, область аккредитации, срок действия на дату работы, "
      "привязка результата к партии или работе. `action=days`: авторские дни и дни качества — "
      "участники, протокол, замечания со сроком и ответственным. `action=sign` — подпись реестра "
      "человеком с ФИО и ролью.",
      {"type": "object", "properties": {
          "action": {"type": "string", "enum": ["letters", "involvement", "days", "sign"],
                     "default": "letters"},
          "letters": {"type": "array", "items": {"type": "object"}},
          "letters_path": {"type": "string"},
          "records": {"type": "array", "items": {"type": "object"}},
          "records_path": {"type": "string"},
          "events": {"type": "array", "items": {"type": "object"}},
          "events_path": {"type": "string"},
          "registry_path": {"type": "string", "description": "для sign: JSON реестра"},
          "object_name": {"type": "string"}, "number": {"type": "string"},
          "by": {"type": "string"}, "role": {"type": "string"}, "reason": {"type": "string"},
          "out_dir": {"type": "string"}, "audit_path": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": []})
def t_correspondence(args, ctx):
    from ..correspondence import (build_involvement_registry, build_letter_registry,
                                  build_quality_days, load_list, sign, to_markdown, write)
    base = Path(ctx.get("base_dir") or ".")
    out_dir = Path(args.get("out_dir") or base / "out" / "correspondence")
    audit = None
    if args.get("audit_path"):
        from ..audit import AuditLog
        audit = AuditLog(Path(args["audit_path"]))
    action = args.get("action") or "letters"

    if action == "sign":
        path = Path(_p(args, "registry_path"))
        document = json.loads(path.read_text(encoding="utf-8"))
        document = sign(document, by=args.get("by") or "", role=args.get("role") or "",
                        reason=args.get("reason") or "", audit_log=audit)
        if document.get("sign_problems"):
            return {"ok": False, "error": "подпись не принята",
                    "problems": document["sign_problems"]}
        files = write(document, out_dir, stem=path.stem)
        return {"ok": True, "kind": document["schema"], "status": document["status"],
                "totals": document["totals"], "confirmed_by": document["confirmed_by"],
                "can_close": document.get("can_close"), "files": files,
                "disclaimer": document["disclaimer"]}

    key = {"letters": "letters", "involvement": "records", "days": "events"}[action]
    items = args.get(key) or []
    problems: list = []
    if not items:
        path_key = {"letters": "letters_path", "involvement": "records_path",
                    "days": "events_path"}[action]
        if args.get(path_key):
            items, problems = load_list(Path(_p(args, path_key)), key)
    builder = {"letters": build_letter_registry, "involvement": build_involvement_registry,
               "days": build_quality_days}[action]
    document = builder(items, object_name=args.get("object_name") or "",
                       number=args.get("number") or "")
    files = write(document, out_dir)
    if audit is not None:
        audit.append("agent:correspondence", "agent", "correspondence.built",
                     f"{document['schema']}:{document.get('number') or 'б/н'}",
                     {"errors": document["totals"]["errors"]})
    payload = {"ok": document["totals"]["errors"] == 0, "kind": document["schema"],
               "status": document["status"], "totals": document["totals"],
               "checks": document["checks"], "rejected": document["rejected"],
               "can_close": document.get("can_close"), "problems": problems, "files": files,
               "disclaimer": document["disclaimer"]}
    payload[key] = document.get(key)
    if action == "letters":
        payload["open_requests"] = document.get("open_requests")
    if (args.get("format") or "json") == "markdown":
        payload["markdown"] = to_markdown(document)
    return payload


@tool("object_pack",
      "Комплект объекта одной командой: собирает все разделы (общий журнал работ, журнал "
      "верификации, документы качества, выдача РД, согласование ИД, письма, лаборатории и "
      "геодезисты, дни качества, чек-листы) в одну папку и считает **один вердикт** — можно ли "
      "предъявлять комплект. Отсутствующий раздел — «нет данных», а не «пройдено»; любая ошибка "
      "в любом разделе блокирует сдачу; сдаётся только подписанное человеком. Готовый "
      "подписанный реестр можно передать ссылкой (`registry`), тогда подпись проверяется, а не "
      "пересобирается. Возвращает разделы, блокеры по разделам с кодами и файлы описи.",
      {"type": "object", "properties": {
          "config": {"type": "object",
                     "description": "{object_name, number, sections: {раздел: {источники}}} "
                                    "— либо передайте config_path"},
          "config_path": {"type": "string"},
          "base_dir": {"type": "string"},
          "out_dir": {"type": "string"},
          "format": {"type": "string", "enum": ["json", "markdown"], "default": "json"}},
       "required": []})
def t_object_pack(args, ctx):
    from ..pack import build_pack, to_markdown
    base = Path(args.get("base_dir") or ctx.get("base_dir") or ".")
    config = args.get("config") or {}
    problems: list = []
    if not config and args.get("config_path"):
        path = Path(_p(args, "config_path"))
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return {"ok": False, "error": f"{path.name}: не читается ({exc.__class__.__name__})"}
    out_dir = Path(args.get("out_dir") or base / "out" / "pack")
    pack = build_pack(config, base_dir=base, out_dir=out_dir)
    payload = {"ok": pack["can_submit"]["allowed"], "kind": pack["schema"],
               "object_name": pack["object_name"], "number": pack["number"],
               "totals": pack["totals"], "sections": pack["sections"],
               "can_submit": pack["can_submit"], "checks": pack["checks"],
               "problems": problems, "files": pack["files"],
               "disclaimer": pack["disclaimer"]}
    if (args.get("format") or "json") == "markdown":
        payload["markdown"] = to_markdown(pack)
    return payload


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pto_agent.cad.mcp_server",
                                 description="MCP-сервер CAD-модуля ИИ-агента ПТО")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1",
                    help="по умолчанию только localhost; наружу — осознанно и с токеном")
    ap.add_argument("--token", default=None,
                    help="токен доступа (или переменная окружения PTO_MCP_TOKEN)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--base-dir", default=".")
    ap.add_argument("--audit", default="out/cad/audit.jsonl")
    args = ap.parse_args(argv)
    ctx = make_ctx(args.base_dir, args.audit)
    if args.transport == "http":
        serve_http(ctx, args.host, args.port, token=args.token)
    else:
        serve_stdio(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
