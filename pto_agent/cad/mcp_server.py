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
from .ops import OPERATIONS, apply_operations, history_entry, validate_operations
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
