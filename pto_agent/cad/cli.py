# -*- coding: utf-8 -*-
"""CLI CAD-модуля: python -m pto_agent cad <команда>."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from ..audit import AuditLog
from ..impact import (assess_impact, impact_chart, impact_to_markdown,
                      write_impact_xlsx)
from ..portfolio import (assess_portfolio, load_acts, portfolio_to_markdown,
                         write_portfolio_reports)
from .diff import diff_drawings
from .external_takeoff import (ExternalTakeoffError, align, load_config,
                               local_positions_from_dxf, local_positions_from_summary,
                               run_external_takeoff, write_report)
from .drawing import Drawing
from .ops import apply_operations, history_entry
from .redline import plan_from_discrepancies
from .render import render_png, render_two_panels
from .takeoff import compare_takeoff, takeoff
from .vision.batch import load_manifest, run_batch, write_reports
from .vision.calibrate import Calibration
from .vision.engine import analyze_scan
from .vision.perspective import PhotoCalibration
from .vision.plan import plan_from_scan
from .template import load_template, validate_drawing

DEFAULT_TEMPLATE = "config/cad_template_corp_A.yaml"
DEFAULT_AUDIT = "out/cad/audit.jsonl"
DEFAULT_HISTORY = "out/cad/history.jsonl"


def _tpl(args):
    return load_template(args.template)


def _dump(obj, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def cmd_inventory(args):
    d = Drawing.open(args.file)
    inv = d.inventory()
    tpl = _tpl(args)
    print(f"Файл:      {inv['file']}")
    print(f"Версия:    DXF {inv['dxf_version']} · единицы: {inv['units']}")
    print(f"Объектов:  {inv['entities_total']} · слоёв: {inv['layers_total']} · "
          f"блоков: {len(inv['blocks_defined'])}")
    print(f"Типы:      {inv['entities_by_type']}")
    print(f"Высота текста: мин {inv['text_heights']['min']} / макс {inv['text_heights']['max']} "
          f"(в печати при {tpl.scale}: {tpl.print_mm(inv['text_heights']['min'] or 0)} мм)")
    tb = d.titleblock(tpl.titleblock_block)
    if tb:
        # вложенные f-строки с одинаковыми кавычками компилируются только с Python 3.12,
        # а поставка и CI заявляют 3.11 — внутреннюю часть собираем отдельно
        fields = ", ".join("%s=%s" % (k, v or "—") for k, v in tb["fields"].items())
        print(f"Штамп:     {tb['block']} · поля: {fields}")
    else:
        print("Штамп:     не найден")
    items = takeoff(d)
    print(f"Объёмы на схеме: {len(items)} позиций")
    for it in items:
        print(f"   {it['code'] or '—':<20} {it['qty']:>8g} {it['unit']:<4} {it['name'][:40]}")
    if args.json:
        _dump({**inv, "titleblock": tb, "takeoff": items}, args.json)
        print(f"JSON: {args.json}")
    return 0


def cmd_validate(args):
    d = Drawing.open(args.file)
    tpl = _tpl(args)
    res = validate_drawing(d, tpl)
    print(f"Шаблон: {res['template']} · файл: {Path(res['file']).name}")
    for i in res["issues"]:
        mark = "✖" if i["severity"] == "blocking" else "!"
        print(f"  {mark} [{i['code']}] {i['message']}")
    print(f"\nБлокирующих: {res['blocking']} · предупреждений: {res['warnings']} · "
          f"выпуск схемы: {'разрешён' if res['ok'] else 'ЗАПРЕЩЁН'}")
    if args.json:
        _dump(res, args.json)
    return 0 if res["ok"] else 1


def cmd_plan(args):
    d = Drawing.open(args.file)
    tpl = _tpl(args)
    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    plan = plan_from_discrepancies(summary, d, tpl)
    plan["validation"] = validate_drawing(d, tpl)
    out = _dump(plan, args.out or "out/cad/plan.json")
    print(f"План операций: {plan['summary']['operations']} операций "
          f"({plan['summary']['clouds']} облаков, {plan['summary']['notes']} пояснений)")
    print(f"На решение человека: {plan['summary']['needs_human_decision']}")
    for o in plan["operations"][:6]:
        print(f"  {o['op_id']} {o['type']:<14} {o['params'].get('text', o['params'].get('entity_handle'))!s:.60}")
    if len(plan["operations"]) > 6:
        print(f"  … ещё {len(plan['operations']) - 6}")
    print(f"JSON: {out}")
    return 0


def cmd_apply(args):
    d = Drawing.open(args.file)
    tpl = _tpl(args)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    ops = plan.get("operations") or plan
    approval = None
    if args.mode == "commit":
        approval = {"by": args.confirm_by, "role": args.role, "comment": args.comment,
                    "ts": datetime.now().isoformat(timespec="seconds")}
    audit = AuditLog(args.audit) if args.mode == "commit" else None
    res = apply_operations(d, ops, tpl, mode=args.mode, out_path=args.out, approval=approval,
                           audit=audit, actor=args.confirm_by or "agent:cad")
    if not res["ok"]:
        print(f"ОТКАЗАНО ({res['blocking']} блокирующих замечаний):")
        for i in res["issues"]:
            if i["severity"] == "blocking":
                print(f"  ✖ [{i['code']}] {i['op_id'] or ''} {i['message']}")
        return 2

    diff = res["diff"]
    print(f"Режим: {res['mode']} · операций применено: {res['applied']}")
    print(f"Изменения: +{diff['added']} новых, ~{diff['modified']} изменённых, "
          f"−{diff['removed']} удалённых")
    if diff["added_by_layer"]:
        print(f"  по слоям: {diff['added_by_layer']}")
    if res["mode"] == "dry_run":
        print("Файл НЕ записан (dry_run). Для записи: --mode commit --confirm-by \"ФИО\"")
    else:
        print(f"Записано: {res['file']}")
        print(f"  объектов было {res['objects_before']} → стало {res['objects_after']} "
              f"(потеряно: {res['objects_lost']})")
        print(f"  sha256: {res['sha256'][:16]}… · запись аудита: {res['audit_hash'][:16]}…")
        hist = Path(args.history)
        hist.parent.mkdir(parents=True, exist_ok=True)
        with hist.open("a", encoding="utf-8") as f:
            f.write(json.dumps(history_entry(res, d, tpl, approval, approval["by"]),
                               ensure_ascii=False) + "\n")
        print(f"  журнал версий: {hist}")
    return 0


def cmd_diff(args):
    a, b = Drawing.open(args.before), Drawing.open(args.after)
    res = diff_drawings(a.doc, b.doc)
    print(f"{Path(args.before).name} → {Path(args.after).name}")
    print(f"Добавлено: {res['added']} · изменено: {res['modified']} · удалено: {res['removed']}")
    for it in res["items"][:15]:
        print(f"  {it['change']:<9} {it['type']:<12} слой {it['layer']:<16} #{it['handle']}")
    if args.json:
        _dump(res, args.json)
    return 0


def cmd_takeoff(args):
    d = Drawing.open(args.file)
    items = takeoff(d)
    print(f"Объёмов на схеме: {len(items)}")
    if not args.summary:
        for it in items:
            print(f"  {it['code'] or '—':<20} {it['qty']:>8g} {it['unit']:<4} {it['name'][:45]}")
        return 0
    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    tpl = _tpl(args)
    res = compare_takeoff(items, summary.get("lines") or [], pct=tpl.takeoff_pct())
    print(f"Допуск сверки: ±{res['tolerance_pct']} % (шаблон {tpl.name})")
    print(f"{'шифр':<20} {'схема':>9} {'акт':>9} {'Δ, %':>7}  источник")
    for m in res["matched"]:
        print(f"{m['code']:<20} {m['cad_qty']:>9g} {m['act_qty']:>9g} "
              f"{m['delta_pct'] if m['delta_pct'] is not None else 0:>7.1f}  {m['match']}")
    print(f"\nЗамечаний: {len(res['issues'])} (критичных {res['critical']})")
    for i in res["issues"]:
        mark = "✖" if i["severity"] == "critical" else "!"
        print(f"  {mark} [{i['type']}] {i['code'] or '—'} — {i['message']}")
    if args.json:
        _dump(res, args.json)
    return 0


def cmd_render(args):
    d = Drawing.open(args.file)
    if args.panels:
        out = render_two_panels(d, args.out or "out/cad/preview.png",
                                zoom=tuple(args.zoom or (4000, 12000, 48000, 50000)))
    else:
        out = render_png(d, args.out or "out/cad/preview.png",
                         zoom=tuple(args.zoom) if args.zoom else None)
    print(f"Предпросмотр: {out}")
    return 0


def _load_anchors(path):
    """Опорные точки из JSON: 4 точки → перспектива (фото), 2 и больше → масштаб (скан)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    anchors = data.get("anchors") or data.get("points") or []
    if not anchors:
        raise SystemExit(f"в {path} нет опорных точек (ожидается ключ anchors)")
    pixels = [a["px"] for a in anchors]
    model = [a["model"] for a in anchors]
    if data.get("mode") == "photo" or len(anchors) >= 4 or data.get("matrix"):
        calib = PhotoCalibration.from_anchors(
            pixels, model, data.get("image_size") or (0, 0),
            rect_size=data.get("rect_size"), rect_model=data.get("rect_model"))
        return calib.as_dict(), "photo"
    calib = Calibration.from_anchors(pixels, model, data.get("image_size") or (0, 0))
    return calib.as_dict(), "flat"


def _vision_kwargs(args):
    kw = {"engine": args.engine, "log_path": args.llm_log}
    if args.texts:
        kw["texts_path"] = args.texts
    if args.truth:
        if args.engine != "mock":
            print("! --truth используется только с --engine mock", file=sys.stderr)
        kw["truth_path"] = args.truth
    if args.calibration:
        kw["calibration"] = json.loads(Path(args.calibration).read_text(encoding="utf-8"))
    if getattr(args, "anchors", None):
        calib, mode = _load_anchors(args.anchors)
        if mode == "photo":
            kw["photo"] = calib
            kw["rectified_path"] = getattr(args, "rectified", None) or "out/cad/photo_rectified_last.png"
        else:
            kw["calibration"] = calib
    if getattr(args, "rectified", None) and "photo" in kw:
        kw["rectified_path"] = args.rectified
    if args.confirm_calibration:
        kw["confirm_calibration"] = True
    if args.provider:
        kw["provider_id"] = args.provider
    return kw


def cmd_scan(args):
    d = Drawing.open(args.file) if args.file else None
    run = analyze_scan(args.scan, drawing=d, **_vision_kwargs(args))
    st = run["stats"]
    kind = "Фото" if run.get("photo") else "Скан"
    shown = run["photo"]["photo_size"] if run.get("photo") else run["image_size"]
    print(f"{kind + ':':<10} {run['image']} ({shown[0]}×{shown[1]} px)")
    print(f"Движок:    {run['engine_used']} (запрошен: {run['engine_requested']}) · "
          f"фрагментов: {run['tiles']} · вызовов модели: {st['llm_calls']}")
    prov = run.get("provider") or {}
    print(f"Провайдер: {prov.get('id', '—')} · модель: {prov.get('model', '—')}")
    cal = run["calibration"]
    if run.get("photo"):
        ph = run["photo"]
        print(f"Фото:      {ph['photo_size'][0]}×{ph['photo_size'][1]} px → "
              f"выпрямлено в {ph['rectified_size'][0]}×{ph['rectified_size'][1]} px · "
              f"остаток на опорных точках {ph['rms_mm']:.1f} мм")
        q = ph["quality"]
        print(f"Качество:  яркость {q['luma_mean']}, блики {q['glare_share'] * 100:.1f} %, "
              f"резкость {q['sharpness']}"
              + (f" · {', '.join(q['warnings'])}" if q["warnings"] else " · замечаний нет"))
    print(f"Калибровка: {cal['method']} · "
          f"{cal.get('mm_per_px', abs(cal['scale_x'])):.4f} ед/px · "
          f"{'подтверждена' if cal['confirmed'] else 'НЕ подтверждена человеком'}")
    print(f"Пометок:   {st['marks']} · пригодны к автоматической обработке: "
          f"{st['actionable']} · вопрос человеку: {st['needs_human']} · "
          f"отклонено проверками: {st['rejected']}")
    for m in run["marks"]:
        flag = "→" if (m["confidence"] >= 0.7 and m["text"].strip()) else "?"
        print(f"  {flag} {m['mark_id']:<4} {m['type']:<10} conf={m['confidence']:.2f} "
              f"bbox={[round(v) for v in m['bbox_px']]} "
              f"«{(m['text'] or '— текст не распознан')[:48]}»")
    for r in run["rejected"]:
        print(f"  ✖ отклонено: {r.get('mark_id')} [{r.get('reason')}] {r.get('detail', '')[:60]}")
    for w in run["warnings"]:
        print(f"  ! {w}")
    if args.texts:
        print(f"Тексты:    введены инженером ({run['texts_source']}) — "
              f"помечены как источник 'human', требуют подтверждения при выпуске")
    if args.json:
        _dump(run, args.json)
        print(f"JSON:      {args.json}")
    return 0


def cmd_redline_from_scan(args):
    """Полный путь: скан → пометки → типизированные операции → проверка → (commit)."""
    d = Drawing.open(args.file)
    tpl = _tpl(args)
    run = analyze_scan(args.scan, drawing=d, **_vision_kwargs(args))
    summary = None
    if args.summary and Path(args.summary).exists():
        summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    plan = plan_from_scan(run, d, tpl, summary, markup_layer=args.layer)
    plan["recognition"] = run
    validation = validate_drawing(d, tpl)

    print(f"Скан:   {args.scan} · распознано пометок: {run['stats']['marks']} "
          f"({run['engine_used']})")
    print(f"План:   операций {plan['summary']['operations']} · конфликтов "
          f"{plan['summary']['conflicts']} · вопросов человеку "
          f"{plan['summary']['needs_human_decision']}")
    for c in plan["conflicts"]:
        print(f"  ⚠ [{c['type']}] {c['mark_id']}: {c['resolution'][:100]}")
    for n in plan["needs_human_decision"]:
        print(f"  ? {n['mark_id']}: {n['reason']} → {n['suggested_action']}")
    if plan["operations"]:
        print("Операции:")
        for o in plan["operations"]:
            print(f"  {o['op_id']} {o['type']:<12} {o['reason'][:88]}")
    _dump(plan, args.out)
    print(f"План сохранён: {args.out}")

    if args.mode == "plan_only":
        print("Ничего не изменено. Проверить и применить: "
              "cad apply --plan <файл> --mode dry_run")
        return 0

    ops = plan.get("operations") or []
    approval = None
    if args.mode == "commit":
        approval = {"by": args.confirm_by, "role": args.role, "comment": args.comment,
                    "ts": datetime.now().isoformat(timespec="seconds")}
    audit = AuditLog(args.audit) if args.mode == "commit" else None
    res = apply_operations(d, ops, tpl, mode=args.mode, out_path=args.out_dxf,
                           approval=approval, audit=audit, actor=args.confirm_by or "agent:cad")
    if not res["ok"]:
        print(f"ОТКАЗАНО ({res['blocking']} блокирующих замечаний):")
        for i in res["issues"]:
            if i["severity"] == "blocking":
                print(f"  ✖ [{i['code']}] {i['op_id'] or ''} {i['message']}")
        return 2
    diff = res["diff"]
    print(f"Режим: {res['mode']} · применено операций: {res['applied']} · "
          f"+{diff['added']} ~{diff['modified']} −{diff['removed']}")
    if res["mode"] == "commit":
        print(f"Записано: {res['file']} · объектов {res['objects_before']}→{res['objects_after']}")
        hist = Path(args.history)
        hist.parent.mkdir(parents=True, exist_ok=True)
        with hist.open("a", encoding="utf-8") as f:
            f.write(json.dumps(history_entry(res, d, tpl, approval, approval["by"]),
                               ensure_ascii=False) + "\n")
        print(f"Аудит: {args.audit} · журнал версий: {hist}")
    else:
        print("Файл НЕ записан (dry_run). Для записи: --mode commit --confirm-by \"ФИО\"")
    return 0


def cmd_takeoff_external(args):
    """Обмер чужим MCP-сервером (PDF и пр.) → наши позиции + сверка с нашим обмером/сметой."""
    try:
        cfg = load_config(args.config)
        report = run_external_takeoff(cfg, args.pdf,
                                      ack_separate_service=args.ack_separate_service,
                                      timeout=args.timeout)
    except ExternalTakeoffError as e:
        print(f"Внешний обмер не выполнен: {e}", file=sys.stderr)
        return 2

    local = None
    if args.file:
        local = local_positions_from_dxf(Drawing.open(args.file))
    elif args.summary and Path(args.summary).exists():
        local = local_positions_from_summary(json.loads(Path(args.summary).read_text(encoding="utf-8")))
    alignment = align(report["positions"], local, pct=args.tolerance_pct) if local is not None else None

    paths = write_report(report, alignment, args.out)
    print(f"Источник:  {report['source']} ({report['license']}) · "
          f"{report['transport']} · протокол {report.get('protocol') or '—'}")
    print(f"Инструмент: {report['tool']} · позиций: {report['count']} "
          f"(с объёмом {report['estimated']}, не оценено {report['not_estimated']})")
    if alignment is not None:
        print(f"Сверка:    совпало {len(alignment['matched'])} из {alignment['local_positions']} · "
              f"расхождений {len(alignment['issues'])} "
              f"(критичных {alignment['critical']})")
        for issue in alignment["issues"][:10]:
            print(f"  • {issue['type']:<22} {issue.get('code') or '—':<16} {issue['message']}")
    if report["warnings"]:
        print(f"Предупреждения: {len(report['warnings'])} (см. отчёт)")
    print(f"Отчёт:     {paths['markdown']} · {paths['json']}")
    return 0


def cmd_impact(args):
    """Пересчитать деньги по готовой сводке выпуска: без повторного распознавания.

    С `--acts` считается портфель: все акты объекта с разбивкой по месяцам.
    """
    if args.acts:
        return _impact_portfolio(args)
    batch = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    pto = json.loads(Path(args.summary).read_text(encoding="utf-8")) if args.summary else None
    conflicts = batch.get("conflicts") or []
    if not conflicts:
        print("В сводке нет конфликтов: считать нечего", file=sys.stderr)
    impact = assess_impact(conflicts, pto,
                           discrepancies=(pto or {}).get("discrepancies")
                           if args.with_background else None)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _dump(impact, out / "impact.json")
    write_impact_xlsx(impact, out / "impact.xlsx")
    (out / "impact.md").write_text(impact_to_markdown(impact), encoding="utf-8")
    chart = impact_chart(impact, out / "impact_chart.png")

    t = impact["totals"]
    money = lambda v: f"{v:,.0f} ₽".replace(",", " ") if v is not None else "—"
    print(f"Объект: {impact.get('object') or '—'} · период: {impact.get('period') or '—'}")
    print(f"Под риском по пометкам выпуска: {money(t['at_risk_rub'])} "
          f"({str(t['share_of_act_pct']).replace('.', ',')} % от акта "
          f"{money(t['act_amount_rub'])})")
    print(f"  завышение против акта: {money(t['markup_rub'])} · "
          f"недополучено: {money(t['underclaim_rub'])}")
    if t.get("background_risk_rub"):
        print(f"Фон ПТО (расхождения объекта): {money(t['background_risk_rub'])}")
    for p_ in impact["positions"]:
        print(f"  {p_['code'] or '—':<20} {money(p_['money_rub']):>16} · "
              f"{p_.get('source') or '—'} · листов {len(p_['sheets']) or '—'}")
    for u in impact["unpriced"]:
        times = f" · повторов {u['occurrences']}" if u.get("occurrences") else ""
        print(f"  ? {u.get('code') or '—'} [{u.get('conflict_type')}]: "
              f"{u.get('note')}{times}")
    print(f"Отчёты: {out / 'impact.xlsx'}")
    print(f"        {out / 'impact.md'}" + (f" · {chart}" if chart else ""))
    return 0


def _impact_portfolio(args):
    """Деньги под риском по всем актам объекта (реестр актов + сводка ПТО)."""
    pto = json.loads(Path(args.summary).read_text(encoding="utf-8")) if args.summary else {}
    acts = load_acts(args.acts)
    out = Path(args.out)
    pf = assess_portfolio(acts, pto, root=Path(".").resolve(),
                          discrepancies_act=args.background_act)
    reports = write_portfolio_reports(pf, out)

    t = pf["totals"]
    money = lambda v: f"{v:,.0f} ₽".replace(",", " ") if v is not None else "—"
    print(f"Объект: {pf.get('object')} · договор {pf.get('contract') or '—'}")
    print(f"Актов: {t['acts_total']} на {money(t['amount_rub'])} · "
          f"проверено выпуском: {t['acts_checked']} на {money(t['amount_checked_rub'])} "
          f"(покрытие {str(t['coverage_pct']).replace('.', ',')} %)")
    print(f"Деньги под риском: {money(t['at_risk_rub'])} "
          f"({str(t['share_of_checked_pct']).replace('.', ',')} % проверенной суммы, "
          f"{str(t['share_of_all_pct']).replace('.', ',')} % всех актов)")
    print(f"  завышение: {money(t['markup_rub'])} · недополучено: {money(t['underclaim_rub'])}")
    if t.get("background_risk_rub"):
        print(f"Фон ПТО (расхождения объекта): {money(t['background_risk_rub'])}")
    for act in pf["acts"]:
        risk = money(act["at_risk_rub"]) if act["status"] == "проверен" else "не оценено"
        print(f"  {act['act_id']:<12} {act['period']:<16} {money(act['amount_rub']):>16}"
              f" · под риском {risk:>16} · {act['status']}")
    for m in pf["months"]:
        risk = money(m["at_risk_rub"]) if m.get("risk_assessed", True) else "не оценено"
        print(f"  {m['label']:<16} актов {len(m['acts'])} · {money(m['amount_rub']):>16}"
              f" · под риском {risk}")
    if t["acts_unchecked"]:
        print(f"Не проверено: {t['acts_unchecked']} акта на {money(t['amount_unchecked_rub'])} "
              f"— это не «0 ₽», а отсутствие проверки")
    print(f"Отчёты: {reports['xlsx']}")
    print(f"        {reports['markdown']} · {reports['json']}"
          + (f" · {reports['chart']}" if reports.get("chart") else ""))
    return 0


def cmd_scan_batch(args):
    """Выпуск целиком: 30–50 листов за сеанс, сводка пометок и конфликтов."""
    summary_data = None
    if args.summary and Path(args.summary).exists():
        summary_data = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    if args.directory:
        from .vision.batch import manifest_from_dir
        sheets = manifest_from_dir(args.directory, default_drawing=args.file,
                                   pattern=args.pattern)
    else:
        sheets = load_manifest(args.manifest)

    print(f"Выпуск: {len(sheets)} листов · движок: {args.engine} · "
          f"шаблон: {Path(args.template).name}")

    def progress(rec):
        state = "ok " if rec.get("ok") else "ERR"
        cached = " (кеш)" if rec.get("from_cache") else ""
        err = "" if rec.get("ok") else f" · {rec.get('error')}"
        print(f"  [{state}] {rec['sheet_id']:<14} пометок {rec.get('marks', 0):>3} · "
              f"операций {rec.get('operations', 0):>2} · конфликтов "
              f"{len(rec.get('conflicts') or []):>2}{cached}{err}")

    summary = run_batch(sheets, engine=args.engine, workers=args.workers,
                        out_dir=args.out, template_path=args.template,
                        summary=summary_data, resume=not args.no_resume,
                        progress=progress)
    paths = write_reports(summary, args.out)

    t = summary["totals"]
    print()
    print(f"Итог: листов {t['sheets']} · успешно {t['ok']} · ошибок {t['failed']} · "
          f"пометок {t['marks']} (авто {t['actionable']}, человеку {t['needs_human']})")
    print(f"      операций {t['operations']} · конфликтов {t['conflicts']} · "
          f"время {t['duration_s']} с" +
          (f" · из кеша {t['sheets_from_cache']}" if t["sheets_from_cache"] else ""))
    if summary["conflicts_by_type"]:
        print("      конфликты: " + ", ".join(f"{k}×{v}" for k, v in
                                              summary["conflicts_by_type"].items()))
    if summary["conflicts_by_code"]:
        top = list(summary["conflicts_by_code"].items())[:5]
        print("      позиции:   " + ", ".join(f"{k} ({v})" for k, v in top))
    if summary["failed_sheets"]:
        print("      ОШИБКИ:")
        for f in summary["failed_sheets"]:
            print(f"        {f['sheet_id']}: {f['error']}")
    for note in summary["notes"]:
        print(f"      ! {note}")
    print(f"Сводка: {paths['xlsx']}")
    print(f"        {paths['markdown']}")
    print(f"        {paths['json']}")
    return 0 if not summary["failed_sheets"] else 1


def build_parser():
    p = argparse.ArgumentParser(prog="pto_agent cad",
                                description="CAD-модуль ИИ-агента ПТО (headless DXF, ezdxf)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, file_required=True):
        if file_required:
            sp.add_argument("--file", required=True, help="путь к DXF")
        sp.add_argument("--template", default=DEFAULT_TEMPLATE)
        return sp

    i = common(sub.add_parser("inventory", help="инвентаризация чертежа"))
    i.add_argument("--json")
    i.set_defaults(func=cmd_inventory)

    v = common(sub.add_parser("validate", help="проверка по корпоративному шаблону"))
    v.add_argument("--json")
    v.set_defaults(func=cmd_validate)

    pl = common(sub.add_parser("plan", help="план операций по расхождениям ПТО"))
    pl.add_argument("--summary", default="out/summary.json")
    pl.add_argument("--out", default="out/cad/plan.json")
    pl.set_defaults(func=cmd_plan)

    ap = common(sub.add_parser("apply", help="применить операции (dry_run / commit)"))
    ap.add_argument("--plan", default="out/cad/plan.json")
    ap.add_argument("--mode", choices=["dry_run", "commit"], default="dry_run")
    ap.add_argument("--out")
    ap.add_argument("--confirm-by", default="")
    ap.add_argument("--role", default="инженер ПТО")
    ap.add_argument("--comment", default="")
    ap.add_argument("--audit", default=DEFAULT_AUDIT)
    ap.add_argument("--history", default=DEFAULT_HISTORY)
    ap.set_defaults(func=cmd_apply)

    df = sub.add_parser("diff", help="сравнить две версии чертежа")
    df.add_argument("--before", required=True)
    df.add_argument("--after", required=True)
    df.add_argument("--json")
    df.set_defaults(func=cmd_diff)

    tk = common(sub.add_parser("takeoff", help="объёмы на схеме и сверка с актом"))
    tk.add_argument("--summary")
    tk.add_argument("--json")
    tk.set_defaults(func=cmd_takeoff)

    rd = common(sub.add_parser("render", help="растровый предпросмотр"))
    rd.add_argument("--out")
    rd.add_argument("--zoom", nargs=4, type=float)
    rd.add_argument("--panels", action="store_true", help="лист + фрагмент в одном файле")
    rd.set_defaults(func=cmd_render)

    def vision_opts(sp, with_apply=False):
        sp.add_argument("--scan", required=True, help="скан/фото листа с пометками (PNG/JPG)")
        sp.add_argument("--engine", choices=["auto", "vlm", "offline", "mock"], default="vlm",
                        help="vlm — локальная VL-модель по API, offline — CV без модели, "
                             "mock — заглушка для тестов/демо")
        sp.add_argument("--texts", help="JSON с текстами пометок, введёнными инженером")
        sp.add_argument("--truth", help="эталонные пометки для --engine mock")
        sp.add_argument("--calibration", help="JSON с готовой калибровкой (ед/px)")
        sp.add_argument("--anchors", help="JSON с опорными точками: 4 точки — фото с "
                                          "телефона (перспектива), 2 и больше — скан")
        sp.add_argument("--rectified", help="куда сохранить выпрямленное фото")
        sp.add_argument("--confirm-calibration", action="store_true",
                        help="инженер проверил привязку скана к чертежу")
        sp.add_argument("--provider", help="id VL-модели из config/llm_providers.yaml")
        sp.add_argument("--llm-log", default="out/cad/llm_calls.jsonl")
        if with_apply:
            sp.add_argument("--summary", default="out/summary.json",
                            help="сводка ПТО для сверки объёмов и «согласовано»")
            sp.add_argument("--layer", default="ПОМЕТКИ")
            sp.add_argument("--out", default="out/cad/scan_plan.json")
            sp.add_argument("--mode", choices=["plan_only", "dry_run", "commit"],
                            default="plan_only")
            sp.add_argument("--out-dxf")
            sp.add_argument("--confirm-by", default="")
            sp.add_argument("--role", default="инженер ПТО")
            sp.add_argument("--comment", default="")
            sp.add_argument("--audit", default=DEFAULT_AUDIT)
            sp.add_argument("--history", default=DEFAULT_HISTORY)
        else:
            sp.add_argument("--json")

    sc = common(sub.add_parser("scan", help="распознать пометки на скане листа"),
                file_required=False)
    sc.add_argument("--file", help="DXF для калибровки скана по габаритам чертежа")
    vision_opts(sc)
    sc.set_defaults(func=cmd_scan)

    rs = common(sub.add_parser("redline-from-scan",
                               help="скан → операции → проверка → применение (HITL)"))
    vision_opts(rs, with_apply=True)
    rs.set_defaults(func=cmd_redline_from_scan)

    bt = sub.add_parser("scan-batch",
                        help="выпуск целиком: 30–50 листов со сканами → сводка пометок")
    src = bt.add_mutually_exclusive_group(required=True)
    src.add_argument("--manifest", help="JSON со списком листов выпуска")
    src.add_argument("--dir", dest="directory", help="каталог со сканами")
    bt.add_argument("--file", help="чертёж по умолчанию (для --dir)")
    bt.add_argument("--pattern", default="*.png", help="маска файлов для --dir")
    bt.add_argument("--out", default="out/cad/batch")
    bt.add_argument("--engine", choices=["auto", "vlm", "offline", "mock"], default="vlm")
    bt.add_argument("--template", default=DEFAULT_TEMPLATE)
    bt.add_argument("--summary", default="out/summary.json",
                    help="сводка ПТО для сверки объёмов и «согласовано»")
    bt.add_argument("--workers", type=int, default=1,
                    help="потоков (только для --engine vlm: рендер офлайн-режима "
                         "не потокобезопасен)")
    bt.add_argument("--no-resume", action="store_true",
                    help="не брать готовые листы из кеша")
    bt.set_defaults(func=cmd_scan_batch)

    ex = sub.add_parser("takeoff-external",
                        help="обмер чужим MCP-сервером и сверка с нашим обмером/сметой")
    ex.add_argument("--config", default="config/external_takeoff.example.yaml",
                    help="конфиг внешнего MCP-обмера (JSON/YAML)")
    ex.add_argument("--pdf", required=True, help="файл, который мерит внешний сервер")
    ex.add_argument("--file", help="наш DXF для сверки объёмов")
    ex.add_argument("--summary", default=None, help="out/summary.json для сверки со сметой")
    ex.add_argument("--tolerance-pct", type=float, default=5.0, help="допуск сверки, %%")
    ex.add_argument("--timeout", type=float, default=None)
    ex.add_argument("--ack-separate-service", action="store_true",
                    help="подтвердить режим «отдельный сервис» для AGPL-обмера")
    ex.add_argument("--out", default="out/cad/external")
    ex.set_defaults(func=cmd_takeoff_external)

    im = sub.add_parser("impact",
                        help="стоимость споров: сводка выпуска → деньги по КС-2")
    im.add_argument("--batch", default="out/cad/batch/batch_summary.json",
                    help="сводка пакетной обработки")
    im.add_argument("--summary", default="out/summary.json",
                    help="сводка ПТО (цены и расхождения)")
    im.add_argument("--out", default="out/cad/impact")
    im.add_argument("--with-background", action="store_true",
                    help="включить в отчёт и расхождения ПТО по объекту (фон)")
    im.add_argument("--acts", default=None,
                    help="реестр актов объекта (data/demo/acts/acts.json): "
                         "тогда считается портфель по актам и месяцам")
    im.add_argument("--background-act", default=None,
                    help="какому акту приписать расхождения ПТО (по умолчанию — "
                         "там, где в реестре помечено background)")
    im.set_defaults(func=cmd_impact)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
