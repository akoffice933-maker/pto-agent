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
import json
import sys
import traceback
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
            result = t["fn"](params.get("arguments") or {}, ctx)
            payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
            is_error = bool(isinstance(result, dict) and result.get("ok") is False)
            return ok({"content": [{"type": "text", "text": payload}], "isError": is_error})
        except Exception as exc:
            return ok({"content": [{"type": "text",
                                    "text": f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=2)}"}],
                       "isError": True})
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return err(-32601, f"метод не поддерживается: {method}")


def make_ctx(base_dir: str | Path = ".", audit_path: str | Path = "out/cad/audit.jsonl") -> dict:
    base = Path(base_dir)
    return {"base_dir": str(base), "audit_path": str(base / audit_path)}


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


def serve_http(ctx, host="0.0.0.0", port=8765):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pto-cad-mcp/0.1"

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type, mcp-protocol-version")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *a):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

        def do_OPTIONS(self):
            self._send(204, b"")

        def do_GET(self):
            if self.path.startswith("/health"):
                return self._send(200, json.dumps({"status": "ok", "tools": len(TOOLS)}).encode())
            if self.path.startswith("/tools"):
                return self._send(200, json.dumps({"tools": _tools_public()},
                                                  ensure_ascii=False).encode())
            if self.path in ("/", "/index.html"):
                return self._send(200, _inspector_html(ctx).encode("utf-8"),
                                  "text/html; charset=utf-8")
            self._send(404, b'{"error":"not found"}')

        def do_POST(self):
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
    print(f"MCP (http) слушает http://{host}:{port}/mcp · инспектор: http://{host}:{port}/", flush=True)
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
 <button onclick="send()">Отправить</button>
 <div class="out" id="out">—</div>
</div>
<script>
async function send(){{
  const q=document.getElementById('q').value, out=document.getElementById('out');
  out.textContent='…';
  try{{
    const r=await fetch('mcp',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:q}});
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
          "action": {"type": "string", "enum": ["draft", "confirm"], "default": "draft"},
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pto_agent.cad.mcp_server",
                                 description="MCP-сервер CAD-модуля ИИ-агента ПТО")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--base-dir", default=".")
    ap.add_argument("--audit", default="out/cad/audit.jsonl")
    args = ap.parse_args(argv)
    ctx = make_ctx(args.base_dir, args.audit)
    if args.transport == "http":
        serve_http(ctx, args.host, args.port)
    else:
        serve_stdio(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
