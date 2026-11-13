# -*- coding: utf-8 -*-
"""CLI MVP ИИ-агента ПТО."""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from .audit import AuditLog, summarize_changes
from .export import (export_discrepancies, export_discrepancies_csv, export_ks2,
                     export_remainder)
from .llm import describe_discrepancies, route
from .norms import NormIndex, load_norms
from .parsers import (WorkbookTooLarge, read_closed_ex, read_estimate_ex,
                      read_journal_ex)
from . import forms as print_forms
from . import stock as stock_ledger
from .pdf_profiles import ProfileError, fact_rules, get_profile, schedule_markers
from .kr_pdf import (derive_boq, extract_facts, extract_levels, find_schedules, kr_report,
                     read_pdf_text)
from .kr_schedules import (coverage as schedules_coverage, read_schedules, schedules_report,
                           to_boq as schedules_to_boq, validate_schedules)
from .takeoff import (build_draft as build_takeoff_draft, confirm as confirm_takeoff,
                      draft_to_markdown as takeoff_to_markdown, export_draft_xlsx,
                      load_map as load_takeoff_map, read_boq)
from .materials import (export_materials_xlsx, materials_to_markdown, orders_from_decision,
                        read_act_materials, read_receipts, reconcile as reconcile_materials)
from .prices import DEFAULT_INDEX, import_prices, load_index, lookup, price_history, prices_to_markdown
from .procurement import (build_request, compare_quotes, decide, export_comparison_xlsx,
                          load_quotes, procurement_to_markdown, request_from_estimate)
from .reconcile import reconcile
from .report import build_report


def _period(entries):
    ds = [e.date for e in entries]
    return (min(ds), max(ds)) if ds else (date.today(), date.today())


def cmd_run(args):
    data = Path(args.data)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(out / "audit.jsonl")

    print("1. Чтение документов…")
    try:
        items, p_est = read_estimate_ex(data / "smeta_v1.xlsx")
        entries, p_journal = read_journal_ex(data / "journal_2026_10.xlsx")
        closed, p_closed = read_closed_ex(data / "acts_previous.xlsx")
    except WorkbookTooLarge as exc:
        print(f"   ОСТАНОВЛЕНО: {exc}")
        return 2
    read_problems = p_est + p_journal + p_closed
    audit.append("agent:parser", "agent", "documents.read", "dataset",
                 {"estimate_items": len(items), "journal_entries": len(entries),
                  "closed_records": len(closed), "read_problems": len(read_problems),
                  # битые ячейки — это факт о документе, а не «ноль»
                  "problems": read_problems[:50]})
    print(f"   смета: {len(items)} позиций · журнал: {len(entries)} записей · "
          f"закрыто ранее: {len(closed)} записей")
    if read_problems:
        print(f"   ⚠ замечаний чтения: {len(read_problems)} — битые ячейки не приняты за 0")
        for problem in read_problems[:5]:
            print(f"     {problem['where']}, строка {problem['row']}: {problem['what']} "
                  f"→ {problem['effect']}")

    print("2. Сверка объёмов и расхождения…")
    rec = reconcile(items, entries, closed, object_name=args.object_name, period=_period(entries))
    audit.append("agent:estimator", "agent", "reconcile.done", "act_draft",
                 {"positions": len(rec.lines_with_qty),
                  "discrepancies": len(rec.discrepancies),
                  "amount_act": rec.amount_act,
                  "amount_safe": rec.amount_act_safe})
    sev = rec.by_severity()
    print(f"   расхождений: {len(rec.discrepancies)} "
          f"(критичных {sev['critical']}, существенных {sev['major']})")

    print("3. Формирование черновика КС-2 и отчётов…")
    ks2 = export_ks2(rec, out / "ks2_draft.xlsx", vat_rate=args.vat)
    disc = export_discrepancies(rec, out / "discrepancies.xlsx")
    export_discrepancies_csv(rec, out / "discrepancies.csv")
    rem = export_remainder(rec, out / "remainder.xlsx")
    audit.append("agent:documenter", "agent", "documents.generated", "ks2_draft",
                 {"files": [ks2.name, disc.name, rem.name], "vat_rate": args.vat})

    print("4. Нормативная проверка (RAG + проверка цитаты)…")
    norms_dir = data / "norms"
    norms_answer = None
    if norms_dir.exists():
        index = NormIndex(load_norms(norms_dir))
        query = args.norm_query
        answer = index.ask(query, as_of=date.today())
        norms_answer = {"query": query, **answer.as_dict()}
        (out / "norm_answer.json").write_text(
            json.dumps(norms_answer, ensure_ascii=False, indent=2), encoding="utf-8")
        audit.append("agent:normative", "agent", "norm.answer", "query",
                     {"query": query, "answer_type": answer.answer_type,
                      "verified": answer.verified})
        print(f"   «{query}» → {answer.answer_type}, "
              f"цитат: {len(answer.citations)}, проверено: {answer.verified}")

    print("5. Отчёт руководителю…")
    report = build_report(rec, out / "audit.jsonl", out / "report.html",
                          vat_rate=args.vat, norms_last=norms_answer)
    audit.append("system", "system", "report.generated", "management_report",
                 {"file": report.name})
    ok, count, msg = audit.verify()
    print(f"   журнал аудита: {count} записей, {msg}")

    summary = {
        "object": rec.object_name,
        "period": [d.isoformat() for d in rec.period],
        "positions_in_act": len(rec.lines_with_qty),
        "amount_estimate": rec.amount_estimate,
        "amount_act": rec.amount_act,
        "amount_act_safe": rec.amount_act_safe,
        "amount_blocked": rec.amount_blocked,
        "vat": round(rec.amount_act * args.vat / 100, 2),
        "vat_claimable": round(rec.amount_act_safe * args.vat / 100, 2),
        "amount_remainder": rec.amount_remainder,
        "lines": _lines_payload(rec),
        "discrepancies": [d.as_dict() for d in rec.discrepancies],
        "by_severity": rec.by_severity(),
        "norms": norms_answer,
        "explanation": describe_discrepancies(rec, use_llm=args.use_llm),
        "outputs": sorted(p.name for p in out.iterdir() if p.is_file()),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding="utf-8")

    print("\n── ИТОГ ────────────────────────────────────────────")
    print(f"  Позиций в акте:      {len(rec.lines_with_qty)}")
    print(f"  По журналу, без НДС: {rec.amount_act:,.2f} ₽")
    print(f"  К предъявлению:      {rec.amount_act_safe:,.2f} ₽ "
          f"(заблокировано {rec.amount_blocked:,.2f} ₽)")
    print(f"  НДС {args.vat:.0f}% от суммы к предъявлению: "
          f"{rec.amount_act_safe * args.vat / 100:,.2f} ₽")
    print(f"  Остаток по смете:    {rec.amount_remainder:,.2f} ₽")
    print(f"  Расхождения:         {len(rec.discrepancies)}")
    print(f"  Результаты в:        {out.resolve()}")
    return summary


def _lines_payload(rec) -> list:
    """Все позиции сметы с объёмами и ограничениями — для отчётов и CAD-сверки."""
    plans = {p["line"].item.code: p for p in rec.act_plan()}
    out = []
    for line in rec.lines:
        plan = plans.get(line.item.code)
        out.append({
            "code": line.item.code,
            "name": line.item.name,
            "unit": line.item.unit,
            "estimate_qty": line.item.qty,
            "closed_before": line.qty_prev_closed,
            "qty_journal": line.qty_journal,
            "remainder": line.qty_remainder,
            "price": line.item.price,
            "amount_claim": (plan or {}).get("amount_claim", 0.0),
            "qty_claim": (plan or {}).get("qty_claim", 0.0),
            "restrictions": (plan or {}).get("restrictions", []),
            # без этого флага КС-2 из summary.json не совпадёт с актом из сверки:
            # позиции с разными единицами в акт не входят
            "unit_mismatch": bool(line.unit_mismatch),
        })
    return out


def cmd_norms(args):
    index = NormIndex(load_norms(Path(args.data) / "norms"))
    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()
    answer = index.ask(args.query, as_of=as_of)
    print(json.dumps({"query": args.query, **answer.as_dict()}, ensure_ascii=False, indent=2))
    return answer


def cmd_audit_verify(args):
    log = AuditLog(Path(args.path))
    # --anchor: сверять с якорем хвоста (ловит удаление последних записей, чего
    # хэш-цепочка сама по себе не видит)
    ok, count, msg = log.verify_anchored() if args.anchor else log.verify()
    print(f"{'OK' if ok else 'НАРУШЕНИЕ'}: записей {count} — {msg}")
    return 0 if ok else 1


def cmd_approve(args):
    """HITL: решение принимает человек. Агент только фиксирует его в журнале."""
    out = Path(args.out)
    summary_path = out / "summary.json"
    if not summary_path.exists():
        print("Нет summary.json — сначала выполните: python -m pto_agent run")
        return 2
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    critical = [d for d in summary["discrepancies"] if d["severity"] == "critical"]
    if args.decision == "approved" and critical and not args.comment:
        print(f"Заблокировано: {len(critical)} критичных расхождений. "
              "Подписание возможно только с письменным обоснованием (--comment).")
        return 3
    audit = AuditLog(out / "audit.jsonl")
    record = audit.append(
        args.by, "human", f"act.{args.decision}", "ks2_draft",
        {"comment": args.comment, "critical_open": len(critical),
         "amount_act": summary["amount_act"], "amount_safe": summary["amount_act_safe"]})
    approval = {
        "decision": args.decision,
        "by": args.by,
        "role": args.role,
        "ts": record["ts"],
        "comment": args.comment,
        "critical_open": len(critical),
        "amount_act": summary["amount_act"],
        "hash": record["hash"],
    }
    (out / "approval.json").write_text(json.dumps(approval, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    ok, count, msg = audit.verify()
    print(f"Решение «{args.decision}» зафиксировано ({args.by}), записей в журнале: {count}, {msg}")
    print(f"Хэш записи: {record['hash'][:16]}… → {out / 'approval.json'}")
    return 0


def cmd_route(args):
    res = route(args.intent, data_class=args.data_class, has_images=args.images,
                complexity=args.complexity)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return res


def cmd_export_1c(args):
    """Проверенная КС-2 → CSV для загрузки в 1С/БИТ.Строительство."""
    from .integrations.one_c import OneCError, export_ks2_csv
    try:
        summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
        res = export_ks2_csv(summary, args.out, quantity=args.quantity)
    except (OneCError, FileNotFoundError) as e:
        print(f"Выгрузка не выполнена: {e}", file=sys.stderr)
        return 2
    print(f"Выгружено в 1С: {res['file']} · строк {res['rows']} · сумма {res['amount']:,.2f} ₽"
          .replace(",", " "))
    print(f"Объём: {res['quantity']} (claim — к предъявлению, journal — по журналу, "
          f"estimate — по смете)")
    return 0


def cmd_import_1c(args):
    """CSV из 1С/Гранд-Сметы → наши строки сметы и/или реестр актов."""
    from .integrations.one_c import (OneCError, acts_document, import_acts_csv,
                                     import_smeta_csv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    code = 0
    if args.smeta:
        try:
            smeta = import_smeta_csv(args.smeta)
            path = out_dir / "smeta_from_1c.json"
            path.write_text(json.dumps(smeta, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Смета: {smeta['count']} строк · предупреждений {len(smeta['warnings'])} → {path}")
        except OneCError as e:
            print(f"Смета не прочитана: {e}", file=sys.stderr)
            code = 2
    if args.acts:
        try:
            imported = import_acts_csv(args.acts)
            doc = acts_document(imported, object_name=args.object_name)
            path = out_dir / "acts_from_1c.json"
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            total = sum(a["amount_rub"] or 0 for a in doc["acts"])
            print(f"Акты: {imported['count']} · сумма {total:,.2f} ₽ · "
                  f"предупреждений {len(imported['warnings'])} → {path}".replace(",", " "))
        except OneCError as e:
            print(f"Акты не прочитаны: {e}", file=sys.stderr)
            code = 2
    if not args.smeta and not args.acts:
        print("Укажите --smeta и/или --acts", file=sys.stderr)
        return 2
    return code


def cmd_sync_1c(args):
    """Синхронизация с 1С по HTTP: акты из 1С → портфель, проверенная КС-2 → обратно."""
    from .integrations.one_c_http import (OneCHttpError, OneCHttpClient, ks2_payload,
                                          load_config, sync)
    try:
        cfg = load_config(args.config)
        client = OneCHttpClient.from_config(cfg)
        summary = None
        if args.summary and Path(args.summary).exists():
            summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
        elif args.push:
            print("Для --push нужна сводка: укажите --summary", file=sys.stderr)
            return 2
        result = sync(client, period=args.period, act_id=args.act_id, summary=summary,
                      quantity=args.quantity, push=args.push, object_name=args.object_name)
    except OneCHttpError as e:
        print(f"Обмен с 1С не выполнен: {e}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "acts_from_1c.json").write_text(
        json.dumps(result["acts_document"], ensure_ascii=False, indent=2), encoding="utf-8")
    if result.get("pushed"):
        payload = ks2_payload(summary, quantity=args.quantity, object_name=args.object_name)
        (out / "ks2_payload.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "sync_log.json").write_text(
        json.dumps({k: v for k, v in result.items() if k != "acts_document"},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(f"Из 1С:    актов {result['acts']['count']} · "
          f"сумма {result['acts']['amount']:,.2f} ₽ · предупреждений "
          f"{len(result['acts']['warnings'])}".replace(",", " "))
    if result.get("pushed"):
        p = result["pushed"]
        print(f"В 1С:     отправлено позиций {p['positions']} · сумма {p['amount']:,.2f} ₽ · "
              f"не оценено {p['not_estimated']}".replace(",", " "))
        print(f"Ключ повтора: {p['idempotency_key']}")
    else:
        print("В 1С:     только чтение (для отправки добавьте --push)")
    print(f"Вызовов:  {len(result['calls'])} · результат в {out}")
    return 0


def cmd_review(args):
    """Сводка проверки: что проверено, что осталось, где нужно решение человека."""
    from .review import collect, write_review
    review = collect(summary_path=args.summary, batch_path=args.batch, acts_path=args.acts,
                     impact_path=args.impact, plan_path=args.plan,
                     materials_path=args.materials, object_name=args.object_name)
    paths = write_review(review, args.out)
    v = review["verdict"]
    print(f"Вердикт:   {v['status']} · подпись {'возможна' if v['can_sign'] else 'невозможна'}")
    print(f"{v['headline']}")
    print(f"Проверок:  {len([c for c in review['checked'] if c['status'] != 'skipped'])} "
          f"выполнено, {len([c for c in review['checked'] if c['status'] == 'skipped'])} пропущено")
    for c in review["checked"]:
        mark = {"ok": "✓", "warning": "!", "fail": "✗", "skipped": "·"}.get(c["status"], "?")
        print(f"  {mark} {c['name']}: {c.get('detail', '')}")
    if review["decisions"]:
        print(f"Решения человека ({len(review['decisions'])}):")
        for d in review["decisions"][:5]:
            print(f"  • [{d['priority']}] {d['question']}")
    print(f"Отчёт:     {paths['markdown']} · {paths['json']}")
    return 0


# ─────────────────────────── цены ресурсов (ФГИС ЦС и др.) ───────────────────────────
def cmd_prices(args) -> int:
    out_dir = Path(args.out)
    audit = AuditLog(Path(args.audit)) if args.audit else None
    action = args.prices_action

    if action == "import":
        result = import_prices(args.file, source=args.source, price_date=args.date,
                               region=args.region, zone=args.zone, out_dir=out_dir,
                               actor=args.actor, audit_log=audit)
        if not result.get("ok"):
            print(f"импорт не выполнен: {result.get('error')}", file=sys.stderr)
            for problem in result.get("problems") or []:
                print("  " + problem, file=sys.stderr)
            return 1
        print(f"Версия цен {result['version']}: позиций {result['items']} "
              f"· источник {result['source']} · дата цены {result['price_date']}")
        print(f"  файл версии: {result['file']} (digest {result['digest']})")
        for problem in result["problems"][:5]:
            print(f"  пропущено: {problem}", file=sys.stderr)
        return 0

    if action == "lookup":
        result = lookup(code=args.code or "", name=args.name or "", as_of=args.as_of,
                        out_dir=out_dir)
        print(prices_to_markdown(lookup_result=result), end="")
        return 0 if result.get("found") else 1

    if action == "history":
        history = price_history(code=args.code or "", name=args.name or "", out_dir=out_dir)
        print(prices_to_markdown(history=history), end="")
        return 0

    if action == "list":
        print(prices_to_markdown(index=load_index(out_dir)), end="")
        return 0

    print("неизвестное действие", file=sys.stderr)
    return 2


# ─────────────────────────── закупка материалов ───────────────────────────
def cmd_procure(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(Path(args.audit)) if args.audit else None
    action = args.procure_action

    if action == "request":
        request = request_from_estimate(args.smeta, object_name=args.object_name,
                                        request_no=args.request_no, need_by=args.need_by)
        (out_dir / "request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
        print(f"Заявка {request['request_no']}: позиций {request['items_count']} "
              f"· источник {request['source']}")
        for item in request["items"]:
            flag = " [назначение вложенного ресурса подтвердить]" if item.get("resource_role_unverified") else ""
            print(f"  {item['code']}: {item['name'][:60]} — {item['qty']} {item['unit']}{flag}")
        if "АРОС" in request.get("source", ""):
            print("  ВНИМАНИЕ: ненумерованные ресурсы внутри расценок не включены; "
                  "это черновик, не готовый заказ.", file=sys.stderr)
        print(f"  файл: {out_dir / 'request.json'}")
        return 0

    if action in {"compare", "decide"}:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        quotes, problems = [], []
        for path in args.quotes:
            loaded, issues = load_quotes(path)
            quotes += loaded
            problems += issues
        comparison = compare_quotes(request, quotes, prefer=args.prefer)
        (out_dir / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
        export_comparison_xlsx(comparison, out_dir / "comparison.xlsx")

        decision = None
        if action == "decide":
            choice = {}
            for chunk in args.choose or []:
                if "=" not in chunk:
                    print(f"неверный формат выбора: {chunk} (ожидается код=поставщик)",
                          file=sys.stderr)
                    return 2
                code, supplier = chunk.split("=", 1)
                choice[code.strip()] = supplier.strip()
            decision = decide(comparison, choice, by=args.by, role=args.role,
                              reason=args.reason or "", audit_log=audit)
            (out_dir / "decision.json").write_text(
                json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
            export_comparison_xlsx(comparison, out_dir / "comparison.xlsx", decision=decision)

        report = out_dir / "procurement.md"
        report.write_text(procurement_to_markdown(
            comparison, decision=decision, request=request if action == "compare" else None),
            encoding="utf-8")
        print(f"Сравнение КП: статус {comparison['status']} · позиций "
              f"{comparison['lines_count']} · сумма {comparison['amount']} руб. · "
              f"экономия {comparison['saving_vs_worst']} руб.")
        if comparison["not_comparable"]:
            print(f"  непригодных КП: {len(comparison['not_comparable'])} "
                  f"(без срока/даты/реквизитов)", file=sys.stderr)
        if comparison["no_quotes"]:
            print(f"  без пригодных предложений: {len(comparison['no_quotes'])} позиций",
                  file=sys.stderr)
        for problem in problems[:5]:
            print("  " + problem, file=sys.stderr)
        if decision:
            print(f"  решение {decision['by']}: {len(decision['decisions'])} позиций, "
                  f"сумма {decision['amount']} руб.; дороже лучшего на "
                  f"{decision['over_best']} руб.")
            for problem in decision["problems"]:
                print("  " + problem, file=sys.stderr)
        print(f"  файлы: {out_dir / 'comparison.xlsx'}, {report}")
        return 0

    print("неизвестное действие", file=sys.stderr)
    return 2


# ─────────────────────────── материалы: заявка → приход → КС-2 ───────────────────────────
def cmd_materials(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.materials_action != "reconcile":
        print("неизвестное действие", file=sys.stderr)
        return 2

    decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
    orders = orders_from_decision(decision)
    if not orders:
        print("в решении нет ни одной позиции — сверять нечего", file=sys.stderr)
        return 1

    receipts, problems = [], []
    for path in args.receipts or []:
        loaded, issues = read_receipts(path)
        receipts += loaded
        problems += issues

    act_materials, act_provided = [], False
    for path in args.act or []:
        loaded, issues = read_act_materials(path)
        act_materials += loaded
        problems += issues
        act_provided = act_provided or bool(loaded)

    report = reconcile_materials(orders, receipts, act_materials if act_provided else None,
                                 object_name=args.object_name or decision.get("object_name") or "",
                                 request_no=decision.get("request_no") or "",
                                 act_provided=act_provided)
    (out_dir / "materials.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    (out_dir / "materials.md").write_text(materials_to_markdown(report), encoding="utf-8")
    export_materials_xlsx(report, out_dir / "materials.xlsx")

    totals = report["totals"]
    print(f"Сверка материалов: статус {report['status']} · расхождений {totals['discrepancies']} "
          f"· материалы в акте: {report['act_materials']}")
    print(f"  заморожено {totals['frozen_rub']:.2f} ₽ · под риском {totals['at_risk_rub']:.2f} ₽ "
          f"· перерасход {totals['overpay_rub']:.2f} ₽ · экономия {totals['saving_rub']:.2f} ₽")
    for item in report["discrepancies"]:
        money = ("—" if item["money"] is None else f"{item['money']:.2f} ₽")
        print(f"  {item['kind']}: {item['code'] or '—'} → {money}")
        print(f"      кому: {item['action']}")
    for problem in problems[:8]:
        print(f"  чтение: {problem}", file=sys.stderr)
    print(f"  файлы: {out_dir / 'materials.json'}, {out_dir / 'materials.xlsx'}, "
          f"{out_dir / 'materials.md'}")
    return 0 if report["status"] != "no_data" else 1


# ─────────────────────────── складской контур ───────────────────────────
def cmd_stock(args) -> int:
    action = args.stock_action
    state = stock_ledger.load(args.state)

    if action == "demo":
        state = stock_ledger.demo_state()
        stock_ledger.save(state, args.state)
        print(f"демо-склад собран: движений {len(state['moves'])}, "
              f"документов {len(state['documents'])} → {args.state}")
        return _stock_balances(state)

    if action == "post":
        documents = json.loads(Path(args.docs).read_text(encoding="utf-8"))
        if isinstance(documents, dict):
            documents = documents.get("documents") or [documents]
        result = stock_ledger.post_many(state, documents,
                                        allow_negative=args.allow_negative,
                                        reason=args.reason or "",
                                        as_of_closed=args.closed_before)
        stock_ledger.save(state, args.state)
        print(f"документов: проведено {result['posted']} · повторно {result['duplicates']} · "
              f"отклонено {result['rejected']} · движений {result['moves']}")
        for item in result["errors"]:
            print(f"  отказано [{item['code']}] {item['what']}: {item['why']}", file=sys.stderr)
        for warning in result["warnings"]:
            print(f"  замечание: {warning}", file=sys.stderr)
        for gap in result["gaps"]:
            print(f"  нужно запросить: {gap['what']} — {gap['ask']}", file=sys.stderr)
        return 0 if result["rejected"] == 0 else 1

    if action == "inventory":
        rows, problems = stock_ledger.read_inventory_file(args.file)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        doc = {"kind": "inventory", "number": args.number or f"ИНВ-3 от {args.date}",
               "date": args.date, "warehouse": args.warehouse,
               "lines": rows}
        result = stock_ledger.post_document(state, doc, allow_negative=args.allow_negative,
                                            reason=args.reason or "")
        stock_ledger.save(state, args.state)
        print(f"инвентаризация {doc['number']}: {result['status']}, движений {len(result['moves'])}")
        for warning in result["warnings"]:
            print(f"  {warning}")
        for item in result["errors"]:
            print(f"  отказано [{item['code']}] {item['what']}: {item['why']}", file=sys.stderr)
        return 0 if result["status"] == stock_ledger.STATUS_OK else 1

    if action == "verify":
        summary = stock_ledger.verify(state)
        print(f"{'OK' if summary['ok'] else 'РАСХОЖДЕНИЯ'}: движений {summary['moves']}, "
              f"документов {summary['documents']}, отклонено {summary['rejected']} — "
              f"{summary['summary']}")
        for problem in summary["problems"]:
            print(f"  [{problem['code']}] {problem['what']}: {problem['why']}", file=sys.stderr)
        return 0 if summary["ok"] else 1

    if action == "m29":
        rows = stock_ledger.m29_rows(state, period=args.period)
        form = print_forms.build_m29(rows["purchases"], rows["writeoffs"], period=args.period,
                                     opening=rows["opening"],
                                     requisites_dict=print_forms.requisites(
                                         period=print_forms._month_label(args.period),
                                         object_name=state.get("object") or ""))
        paths = print_forms.write(form, args.out)
        print(f"М-29 из движений склада за {args.period}: строк {len(form['rows'])} · "
              f"остаток на начало {'есть' if form['totals']['opening_given'] else 'НЕ ЗАДАН'}")
        for label, value in print_forms.totals_ru(form):
            print(f"  {label}: {value}")
        for warning in form["warnings"]:
            print(f"  расхождение: {warning}", file=sys.stderr)
        print(f"  файлы: {paths['docx']}, {paths['xlsx']}, {paths['md']}")
        return 0

    if action == "print":
        documents = json.loads(Path(args.docs).read_text(encoding="utf-8"))
        if isinstance(documents, dict) and "documents" in documents:
            documents = documents["documents"]
        elif isinstance(documents, dict):
            documents = [documents]
        mapping = {"receipt": "m4", "issue": "m11", "limit": "m8", "transfer": "m15",
                   "inventory": "inv3", "writeoff": "writeoff",
                   "daval_receipt": "daval", "daval_return": "daval"}
        made = 0
        for doc in documents:
            kind = args.form or mapping.get(str(doc.get("kind") or ""))
            if not kind:
                print(f"  {doc.get('number')}: вид «{doc.get('kind')}» бланка не имеет",
                      file=sys.stderr)
                continue
            try:
                form = print_forms.build_stock_form(kind, doc, state=state or None)
            except ValueError as error:
                print(f"  {doc.get('number')}: {error}", file=sys.stderr)
                continue
            paths = print_forms.write(form, args.out)
            made += 1
            print(f"  {form['code']:<13} {form['document']['number']:<18} строк "
                  f"{len(form['rows'])} → {Path(paths['docx']).name}")
            for warning in form["warnings"]:
                print(f"      замечание: {warning}", file=sys.stderr)
            for gap in form["gaps"]:
                print(f"      нужно запросить: {gap['what']} — {gap['ask']}", file=sys.stderr)
        print(f"форм напечатано: {made} · {form['draft_notice'] if made else '—'}")
        return 0 if made else 1

    if action == "moves":
        moves = stock_ledger.moves_of(state, item=args.item, warehouse=args.warehouse)
        for move in moves[-args.limit:]:
            print(f"{move['date']} {move['kind']:<13} {move['doc_id']:<18} "
                  f"{move['warehouse']:<6} {move['item']:<18} "
                  f"{move['qty']:>9.3f} {move['unit']:<3} "
                  f"{(move['price'] if move['price'] is not None else '—'):>10} "
                  f"{'[давальческий]' if move['daval'] else ''}")
        print(f"всего движений: {len(moves)}")
        return 0

    if getattr(args, "all_contours", False):
        daval = None  # оба контура: собственный и давальческий
    else:
        daval = bool(getattr(args, "daval", False))
    return _stock_balances(state, as_of=getattr(args, "as_of", None),
                           warehouse=getattr(args, "warehouse", None), daval=daval)


def _stock_balances(state: dict, *, as_of: str | None = None, warehouse: str | None = None,
                    daval: bool | None = False) -> int:
    """Остатки. По умолчанию — только собственные: давальческие живут на 003 отдельно."""
    rows = stock_ledger.balances(state, as_of=as_of, warehouse=warehouse, daval=daval)
    if not rows:
        print("остатков нет")
        return 0
    print(f"{'склад':<8} {'номенклатура':<20} {'кол-во':>12} {'ед.':<4} {'цена':>10} {'сумма':>14}")
    total = 0.0
    for row in rows:
        amount = row["amount"] or 0.0
        total += amount
        print(f"{row['warehouse']:<8} {row['item']:<20} {row['qty']:>12.3f} {row['unit']:<4} "
              f"{(row['price'] if row['price'] is not None else '—'):>10} {amount:>14,.2f}"
              f"{'  (003)' if row['daval'] else ''}")
    print(f"{'итого по остаткам:':<46} {total:>14,.2f} ₽")
    return 0


# ─────────────────────────── печатные формы КС-3 / КС-6а / М-29 ───────────────────────────
def cmd_forms(args) -> int:
    req = {name: value for name, value in (("Объект", args.object_name),
                                           ("Договор", args.contract),
                                           ("Заказчик", args.customer),
                                           ("Подрядчик", args.contractor),
                                           ("Номер акта", getattr(args, "number", ""))) if value}
    try:
        if getattr(args, "stock", None):
            state = stock_ledger.load(args.stock)
            rows = stock_ledger.m29_rows(state, period=args.period)
            form = print_forms.build_m29(
                rows["purchases"], rows["writeoffs"], period=args.period,
                opening=rows["opening"],
                requisites_dict=print_forms.requisites(
                    period=print_forms._month_label(args.period),
                    object_name=req.get("Объект") or state.get("object") or "",
                    contract=req.get("Договор", ""),
                    customer=req.get("Заказчик", ""),
                    contractor=req.get("Подрядчик", "")))
        else:
            form = print_forms.build_from_files(
                args.form, period=args.period,
                acts_path=getattr(args, "acts", None) or getattr(args, "summary", None),
                purchases_path=getattr(args, "purchases", None),
                writeoffs_path=getattr(args, "writeoffs", None),
                norms_path=getattr(args, "norms", None), works_path=getattr(args, "works", None),
                opening_path=getattr(args, "opening", None),
                req=req, vat_rate=getattr(args, "vat_rate", None))
    except ValueError as error:
        print(f"форма: {error}", file=sys.stderr)
        return 2

    paths = print_forms.write(form, args.out)
    print(f"{form['code']} за {form['period_label'] or form['period']}: строк {len(form['rows'])} · "
          f"{form['draft_notice']}")
    for label, value in print_forms.totals_ru(form):
        print(f"  {label}: {value}")
    for warning in form["warnings"]:
        print(f"  расхождение: {warning}", file=sys.stderr)
    for gap in form["gaps"]:
        print(f"  нужно запросить: {gap['what']} — {gap['ask']}", file=sys.stderr)
    print(f"  файлы: {paths['docx']}, {paths['xlsx']}, {paths['md']}, {paths['json']}")
    return 0


# ─────────────────────────── takeoff → ведомость объёмов с ценами ───────────────────────────
def cmd_takeoff(args) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    action = args.takeoff_action

    if action == "draft":
        boq, problems = read_boq(args.boq)
        if not boq:
            print("в выгрузке объёмов нет ни одной строки", file=sys.stderr)
            for problem in problems:
                print("  " + problem, file=sys.stderr)
            return 1
        draft = build_takeoff_draft(boq, mapping=load_takeoff_map(args.map),
                                    prices_dir=args.prices_dir, as_of=args.as_of,
                                    object_name=args.object_name, section=args.section)
        (out_dir / "draft.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        (out_dir / "draft.md").write_text(takeoff_to_markdown(draft), encoding="utf-8")
        export_draft_xlsx(draft, out_dir / "draft.xlsx")
        totals = draft["totals"]
        print(f"Ведомость объёмов {draft['section']}: строк {totals['lines']} · с ценой "
              f"{totals['priced_lines']} · на проверку {totals['review_lines']} · не включено "
              f"{totals['excluded_lines']}")
        amount = totals["amount_rub"]
        high = totals["amount_high_rub"]
        print(f"  итог по строкам с известной ценой: "
              f"{'—' if amount is None else f'{amount:.2f}'} ₽"
              + (f" (высокая уверенность: {high:.2f} ₽)" if high is not None else ""))
        for item in draft["excluded"]:
            print(f"  не включено: {item['element'][:60]} — {item['reason']}", file=sys.stderr)
        for problem in problems[:5]:
            print(f"  чтение: {problem}", file=sys.stderr)
        print(f"  файлы: {out_dir / 'draft.json'}, {out_dir / 'draft.xlsx'}, {out_dir / 'draft.md'}")
        return 0

    if action == "tz":
        from .tz_tables import extract_file, write_report
        report = extract_file(args.file, cache=args.cache)
        paths = write_report(report, out_dir)
        totals = report["totals"]
        print(f"ТЗ: страниц {report['pages_total']} · таблиц "
              f"{', '.join(f'{k} — {v}' for k, v in (report['tables_found'] or {}).items()) or 'нет'}")
        print(f"  объёмов: {totals['lines']} (высокая {totals['high']} · средняя "
              f"{totals['medium']} · низкая {totals['low']}) · «не оценено» "
              f"{totals['not_estimated']} · не разобрано {totals['unparsed']}")
        for check in report["checks"]:
            mark = "✓" if check["result"] == "сходится" else "!"
            print(f"  {mark} {check['name'][:70]}: {check['result']}")
        for item in report["not_estimated"]:
            print(f"  не оценено: {item['description'][:60]} — {item['why']}", file=sys.stderr)
        if args.tz_no_draft:
            print(f"  файлы: {paths['json']}, {paths['boq']}, {paths['md']}")
            return 0
        boq, problems = read_boq(paths["boq"])
        if not boq:
            print("в выгрузке объёмов нет ни одной строки", file=sys.stderr)
            for problem in problems:
                print("  " + problem, file=sys.stderr)
            return 1
        draft = build_takeoff_draft(boq, mapping=load_takeoff_map(args.map),
                                    prices_dir=args.prices_dir, as_of=args.as_of,
                                    object_name=args.object_name, section=args.section)
        (out_dir / "draft.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        (out_dir / "draft.md").write_text(takeoff_to_markdown(draft), encoding="utf-8")
        export_draft_xlsx(draft, out_dir / "draft.xlsx")
        dt = draft["totals"]
        print(f"  ведомость: строк {dt['lines']} · с ценой {dt['priced_lines']} · на проверку "
              f"{dt['review_lines']} · не включено {dt['excluded_lines']}")
        amount, high = dt["amount_rub"], dt["amount_high_rub"]
        print("  итог по строкам с известной ценой: "
              + ("—" if amount is None else f"{amount:.2f}") + " ₽"
              + (" (высокая уверенность: —" if high is None
                 else f" (высокая уверенность: {high:.2f}") + " ₽)")
        print(f"  файлы: {paths['json']}, {paths['boq']}, {paths['md']}, "
              f"{out_dir / 'draft.json'}")
        return 0

    if action == "pdf":
        cache = Path(args.cache) if args.cache else out_dir / f"{Path(args.file).stem}_pages.json"
        doc = read_pdf_text(args.file, cache=cache)
        try:
            profile = get_profile(args.profile, args.profiles)
        except ProfileError as error:
            print(f"профиль: {error}", file=sys.stderr)
            return 2
        schedules = find_schedules(doc, markers=schedule_markers(profile))
        levels = extract_levels(doc)
        facts = extract_facts(doc, rules=fact_rules(profile))
        derived = derive_boq(facts, levels=levels, profile=profile)
        draft = build_takeoff_draft(derived["lines"], mapping=load_takeoff_map(args.map),
                                    prices_dir=args.prices_dir, as_of=args.as_of,
                                    object_name=args.object_name, section=args.section)
        (out_dir / "draft.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        (out_dir / "draft.md").write_text(takeoff_to_markdown(draft), encoding="utf-8")
        export_draft_xlsx(draft, out_dir / "draft.xlsx")
        payload = {"schema": "pto-agent/kr-run@1", "file": doc["file"],
                   "profile": {k: profile[k] for k in ("name", "title", "validated_on")},
                   "pages_total": doc["pages_total"], "pages_with_text": doc["pages_with_text"],
                   "pages_without_text": doc["pages_without_text"],
                   "read_errors": doc["read_errors"], "schedules": schedules, "levels": levels,
                   "facts": facts["facts"],
                   "derived": {k: derived[k] for k in ("lines", "needs_input", "formulas")}}
        (out_dir / "kr_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
        (out_dir / "kr_report.md").write_text(
            kr_report(doc, schedules, facts, derived, object_name=args.object_name, draft=draft,
                      profile=profile),
            encoding="utf-8")

        print(f"профиль: {profile['name']} ({profile['title']})"
              + ("" if profile["validated"] else " — на реальном документе не проверялся"))
        print(f"PDF: страниц {doc['pages_total']} · с текстом {doc['pages_with_text']} · "
              f"без текста {len(doc['pages_without_text'])}")
        print(f"  ведомости: {', '.join(schedules['found']) or 'не найдены'} "
              f"| нет: {', '.join(schedules['missing'])}")
        print(f"  фактов из текста: {len(facts['facts'])} · перекрытий в томе: "
              f"{levels['levels_count']} ({', '.join(levels['levels'])})")
        for line in derived["lines"]:
            print(f"  объём: {line['description']} — {line['qty']:g} {line['unit']} "
                  f"({line['confidence']}): {line['formula']}")
        for need in derived["needs_input"]:
            print(f"  нужно спросить: {need['what']} — {need['ask']}", file=sys.stderr)
        totals = draft["totals"]
        print(f"  итог по строкам с ценой: {totals['amount_rub']} ₽ · "
              f"не включено строк: {totals['excluded_lines']}")
        print(f"  файлы: {out_dir / 'kr_report.md'}, {out_dir / 'draft.xlsx'}, "
              f"{out_dir / 'draft.json'}")
        return 0

    if action == "schedules":
        doc = read_schedules(args.schedules)
        facts = {}
        derived = None
        if args.pdf_report and Path(args.pdf_report).exists():
            pdf_payload = json.loads(Path(args.pdf_report).read_text(encoding="utf-8"))
            facts = {"by_name": {f["name"]: f for f in pdf_payload.get("facts") or []}}
            derived = pdf_payload.get("derived")
        validation = validate_schedules(doc, facts)
        boq = schedules_to_boq(doc, validation)
        # покрытие считаем только по ведомостям: расчёт по габаритам не делает конструкцию
        # «покрытой ведомостью»
        cover = schedules_coverage(doc)
        draft = build_takeoff_draft(boq["lines"], mapping=load_takeoff_map(args.map),
                                    prices_dir=args.prices_dir, as_of=args.as_of,
                                    object_name=args.object_name or doc.get("object") or "",
                                    section=args.section)
        (out_dir / "draft.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
        (out_dir / "draft.md").write_text(takeoff_to_markdown(draft), encoding="utf-8")
        export_draft_xlsx(draft, out_dir / "draft.xlsx")
        payload = {"schema": "pto-agent/kr-schedules-run@1",
                   "object": doc.get("object"), "project": doc.get("project"),
                   "source": doc.get("source"),
                   "needs_verification": bool(doc.get("needs_verification")),
                   "validation": validation, "boq": boq, "coverage": cover,
                   "draft_totals": draft["totals"]}
        (out_dir / "kr_schedules_report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "kr_schedules_report.md").write_text(
            schedules_report(doc, validation, boq, draft=draft, derived_from_pdf=derived,
                             cover=cover), encoding="utf-8")

        print(f"Ведомости: листов {len(doc.get('sheets') or [])} · таблиц "
              f"{sum(len(s.get('tables') or []) for s in doc.get('sheets') or [])}"
              + (" · требуют сверки с PDF" if doc.get("needs_verification") else ""))
        for kind, slot in boq["totals_by_kind"].items():
            qty = ", ".join(f"{v:g} {u}" for u, v in slot["qty"].items())
            print(f"  {kind}: строк {slot['lines']} · {qty}")
        for finding in validation["findings"]:
            print(f"  замечание [{finding['type']}] {finding['where']}: {finding['detail']}",
                   file=sys.stderr)
        for item in cover["missing"]:
            print(f"  нет в ведомостях: {item['position']} — {item['ask']}", file=sys.stderr)
        totals = draft["totals"]
        print(f"  ведомость с ценами: строк {totals['lines']} · с ценой {totals['priced_lines']}"
              f" · без цены {totals['no_price_lines']} · не включено {totals['excluded_lines']}")
        print(f"  итог по строкам с ценой: {totals['amount_rub']} ₽")
        for item in draft["excluded"]:
            print(f"  не включено: {item['element'][:60]} — {item['reason']}", file=sys.stderr)
        print(f"  файлы: {out_dir / 'kr_schedules_report.md'}, {out_dir / 'draft.md'}, "
              f"{out_dir / 'draft.xlsx'}")
        return 0

    if action == "confirm":
        draft = json.loads(Path(args.draft).read_text(encoding="utf-8"))
        changes = json.loads(Path(args.changes).read_text(encoding="utf-8")) if args.changes else {}
        for chunk in args.set or []:
            if "=" not in chunk:
                print(f"неверный формат --set: {chunk} (ожидается «элемент=количество»)",
                      file=sys.stderr)
                return 2
            element, qty = chunk.rsplit("=", 1)
            changes.setdefault(element.strip(), {})["qty"] = qty.strip()
        audit = AuditLog(Path(args.audit)) if args.audit else None
        draft = confirm_takeoff(draft, changes, by=args.by, role=args.role,
                                reason=args.reason or "", audit_log=audit)
        (out_dir / "draft.json").write_text(json.dumps(draft, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        (out_dir / "draft.md").write_text(takeoff_to_markdown(draft), encoding="utf-8")
        export_draft_xlsx(draft, out_dir / "draft.xlsx")
        confirmed = draft["confirmed_by"]
        print(f"Ведомость подтверждена: {confirmed['by']} ({confirmed['role']}), "
              f"правок {len(confirmed['changes'])} · итог {draft['totals']['amount_rub']} ₽")
        if confirmed.get("reason_missing"):
            print("  причина не указана — в журнале будет пометка reason_missing",
                  file=sys.stderr)
        for problem in confirmed["problems"]:
            print(f"  {problem}", file=sys.stderr)
        print(f"  файлы: {out_dir / 'draft.json'}, {out_dir / 'draft.xlsx'}")
        return 0

    print("неизвестное действие", file=sys.stderr)
    return 2


# ─────────────────────────── журнал аудита ───────────────────────────
def cmd_audit(args) -> int:
    log = AuditLog(Path(args.path))
    if args.audit_action == "anchor":
        body = log.anchor()
        print(f"якорь записан: записей {body['entries']}, хэш {body['head_hash'][:16]}… "
              f"→ {log.anchor_path()}")
        return 0 if body.get("chain_ok") else 1
    if args.audit_action == "list":
        rows = summarize_changes(log.history(entity=args.entity, limit=args.limit))
        print(f"записей: {len(rows)}")
        for row in rows:
            fields = ",".join(row["fields"][:4])
            print(f"  {row['ts']} {row['hash']} {row['actor']:<18} {row['action']:<26} "
                  f"{row['entity'][:34]:<34} {fields}")
            if row["reason"]:
                print(f"      причина: {row['reason']}")
        return 0
    if args.audit_action == "show":
        record = log.find(args.hash)
        if not record:
            print("запись не найдена", file=sys.stderr)
            return 1
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0
    if args.audit_action == "export":
        rows = summarize_changes(log.history(entity=args.entity))
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Аудит"
        ws.append(["Когда", "Хэш", "Кто", "Действие", "Сущность", "Поля", "Причина",
                   "Основание"])
        for row in rows:
            ws.append([row["ts"], row["hash"], row["actor"], row["action"], row["entity"],
                       ", ".join(row["fields"]), row["reason"], row["ref"]])
        wb.save(out)
        ok, count, message = log.verify()
        print(f"выгружено {len(rows)} записей → {out}; проверка цепочки: {message}")
        return 0 if ok else 1
    print("неизвестное действие", file=sys.stderr)
    return 2



def _tk_registry(args, out_dir: Path, audit) -> int:
    """Реестр ТК объекта: объект → карта → статус → кто проверил."""
    from .tk_registry import build as build_registry, load_drafts, verify, write as write_registry

    paths = list(getattr(args, "paths", None) or []) or [out_dir]
    drafts, problems = load_drafts(paths)
    registry = build_registry(drafts, problems=problems)
    files = write_registry(registry, out_dir)
    totals = registry["totals"]

    print(f"реестр ТК: объектов {totals['objects']} · черновиков {totals['drafts']} · "
          f"проверено инженером {totals['confirmed']} · не проверено {totals['not_checked']}")
    for slot in registry["objects"]:
        slot_totals = slot["totals"]
        print(f"  · {slot['object_name']}: {slot['state']} "
              f"(ТК {slot_totals['tk']}, ППР {slot_totals['ppr']}, "
              f"запросов данных {slot_totals['needs_input']})")
    for item in registry["changed_after_confirm"]:
        print(f"  ⚠ {item['object_name']} · {item['file']}: содержание поправлено после проверки "
              f"({item['by'] or 'ФИО нет'}) — проверка недействительна")
    for problem in problems:
        print(f"  ⚠ {problem}")
    integrity = verify(registry)
    for problem in integrity:
        print(f"  ⚠ {problem}")
    print(f"  файлы: {files['json']}, {files['markdown']}, {files['xlsx']}")
    print("  реестр показывает состояние черновиков, а не право применять карты на объекте")

    if audit is not None:
        audit.append("agent:tk", "agent", "tk.registry_built", "tk:registry",
                     {"objects": totals["objects"], "drafts": totals["drafts"],
                      "confirmed": totals["confirmed"],
                      "changed_after_confirm": totals["changed_after_confirm"]})

    if registry["changed_after_confirm"] or problems or integrity:
        return 2
    if getattr(args, "strict", False) and totals["not_checked"]:
        print(f"  ⚠ --strict: карт без проверки человеком — {totals['not_checked']}")
        return 2
    return 0


def cmd_tk(args) -> int:
    """Черновик ТК или ППР: что известно — заполняем, чего нет — запрашиваем с адресатом."""
    from .tk import build_ppr_draft, build_tk_draft, confirm as confirm_tk, to_markdown, write

    out_dir = Path(getattr(args, "out", "out/tk"))
    object_name = getattr(args, "object_name", "") or ""
    positions = None
    if getattr(args, "estimate", ""):
        from .parsers import read_estimate_ex
        items, problems = read_estimate_ex(Path(args.estimate))
        positions = [{"name": i.name, "qty": i.qty, "unit": i.unit,
                      "source": f"смета {Path(args.estimate).name}"} for i in items]
        if problems:
            print(f"   ⚠ замечаний чтения сметы: {len(problems)}")

    audit = AuditLog(out_dir / "audit.jsonl") if getattr(args, "audit", None) else None
    action = args.tk_action

    if action == "registry":
        return _tk_registry(args, out_dir, audit)

    if action == "draft":
        draft = build_tk_draft(work_type_id=args.work_type or None,
                               work_text=getattr(args, "text", "") or None,
                               object_name=object_name,
                               positions=positions, norms_dir=getattr(args, "norms", None))
    else:                                        # ppr
        draft = build_ppr_draft(object_name=object_name, positions=positions)

    if action == "confirm":
        draft_path = Path(args.draft)
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        filled = json.loads(Path(args.filled).read_text(encoding="utf-8")) if args.filled else {}
        draft = confirm_tk(draft, by=args.by, role=args.role, filled=filled, reason=args.reason,
                           audit_log=audit)
        if draft.get("confirm_problems"):
            print("   ОСТАНОВЛЕНО:", "; ".join(draft["confirm_problems"]))
            return 2
        files = write(draft, out_dir, stem=draft_path.stem)
        print(f"проверено человеком: {args.by} ({args.role}) · внесено пунктов: "
              f"{len(draft['confirmed_by']['filled_items'])}")
        print(f"осталось запросить: {draft['totals']['needs_input']} разделов")
        print(f"файлы: {files['json']}, {files['docx']}")
        return 0

    files = write(draft, out_dir)
    totals = draft["totals"]
    print(f"{draft['title']}: {draft.get('object_name') or 'объект не указан'}")
    if draft.get("work_type"):
        print(f"  вид работ: {draft['work_type']['name']}")
    print(f"  разделов: {totals['sections']} · заполнено: {totals['filled_items']} · "
          f"требуют данных: {totals['needs_input']}")
    print(f"  нормы: проверено {totals['norms_verified']} из {totals['norms_total']}")
    for need in draft["needs_input"][:6]:
        print(f"    ⚠ {need['what']} → {need['ask']}")
    print(f"  файлы: {files['json']}, {files['docx']}")
    print("  это черновик: не утверждённая ТК, подписывает ИТР после проверки")
    if audit is not None:
        audit.append("agent:tk", "agent", f"{draft['kind']}.draft_built",
                     f"{draft['kind']}:{object_name or 'без объекта'}",
                     {"sections": totals["sections"], "needs_input": totals["needs_input"],
                      "norms_verified": totals["norms_verified"]})
    return 0



def cmd_cumulative(args) -> int:
    """Накопительная ведомость выполненных работ: нарастающим итогом по актам."""
    from .cumulative import build_ledger, load_acts, load_estimate, write

    out_dir = Path(args.out)
    acts_data, problems = load_acts(Path(args.acts))
    if problems:
        for problem in problems:
            print(f"   ✗ {problem}", file=sys.stderr)
        return 2
    estimate = load_estimate(Path(args.estimate)) if args.estimate else {}
    ledger = build_ledger(acts_data, estimate=estimate)
    files = write(ledger, out_dir)
    totals = ledger["totals"]

    print(f"накопительная ведомость: {ledger['object'] or 'объект не указан'} · "
          f"актов {totals['acts']} · периодов {totals['periods']} · позиций {totals['rows']}")
    print(f"  сумма актов {totals['amount_acts_rub']:,.2f} ₽ · сумма по позициям "
          f"{totals['amount_positions_rub']:,.2f} ₽ · расхождение "
          f"{totals['difference_rub']:,.2f} ₽")
    for check in ledger["checks"]:
        mark = "✗" if check["level"] == "error" else "⚠"
        print(f"  {mark} {check['code']}: {check['message']}")
    print(f"  файлы: {files['json']}, {files['markdown']}, {files['xlsx']}")
    if totals["errors"]:
        return 2
    if args.strict and totals["warnings"]:
        print(f"  ⚠ --strict: предупреждений {totals['warnings']}")
        return 2
    return 0


def cmd_atn(args) -> int:
    """АТН: акт на вновь возникающие и дополнительные работы (только с основанием)."""
    from .atn import build_atn, confirm as confirm_atn, to_markdown, write
    from .cumulative import build_ledger, load_acts, load_estimate

    out_dir = Path(args.out)
    audit = AuditLog(out_dir / "audit.jsonl") if getattr(args, "audit", None) else None

    if getattr(args, "atn_action", None) == "confirm":
        path = Path(args.atn)
        atn = json.loads(path.read_text(encoding="utf-8"))
        atn = confirm_atn(atn, by=args.by, role=args.role, reason=args.reason, audit_log=audit)
        if atn.get("confirm_problems"):
            print("   ОСТАНОВЛЕНО:", "; ".join(atn["confirm_problems"]))
            return 2
        files = write(atn, out_dir, stem=path.stem.replace("atn_", ""))
        print(f"проверено человеком: {args.by} ({args.role}) · позиций "
              f"{atn['confirmed_by']['positions']} · к предъявлению "
              f"{atn['totals']['to_claim_rub']:,.2f} ₽")
        print(f"   статус: {atn['status']}")
        print(f"  файлы: {files['json']}, {files['docx']}")
        return 0

    items_path = Path(args.items)
    try:
        items = json.loads(items_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"   ✗ {items_path.name}: не читается ({exc.__class__.__name__})", file=sys.stderr)
        return 2
    if isinstance(items, dict):
        items = items.get("items") or items.get("positions") or []

    ledger = {}
    if getattr(args, "acts", None):
        acts_data, problems = load_acts(Path(args.acts))
        for problem in problems:
            print(f"   ⚠ {problem}")
        if acts_data:
            ledger = build_ledger(acts_data,
                                  estimate=load_estimate(Path(args.estimate))
                                  if getattr(args, "estimate", None) else {})
    estimate = load_estimate(Path(args.estimate)) if getattr(args, "estimate", None) else {}

    atn = build_atn(items, number=args.number, object_name=args.object_name or "",
                    contract=args.contract or "", customer=args.customer or "",
                    contractor=args.contractor or "", ledger=ledger, estimate=estimate,
                    reason=args.reason or "")
    files = write(atn, out_dir)
    totals = atn["totals"]
    print(f"АТН {atn['number'] or 'б/н'}: {atn['object_name'] or 'объект не указан'}")
    print(f"  к предъявлению {totals['to_claim_rub']:,.2f} ₽ по {totals['included']} позициям")
    for key, mark in (("needs_basis", "⚠"), ("no_price", "⚠"), ("rejected", "✗")):
        for row in atn[key]:
            print(f"  {mark} {key}: {row.get('code') or row.get('name') or '—'} — "
                  f"{row.get('why')}")
    if totals["needs_basis"]:
        print(f"  требует основания: {totals['needs_basis']} поз. на "
              f"{totals['needs_basis_rub']:,.2f} ₽ — в акт не вошли")
    print(f"  файлы: {files['json']}, {files['docx']}")
    print("  это черновик: акт подписывают заказчик и подрядчик, агент его не согласовывает")
    if audit is not None:
        audit.append("agent:atn", "agent", "atn.draft_built", f"atn:{atn['number'] or 'б/н'}",
                     {"to_claim_rub": totals["to_claim_rub"], "included": totals["included"],
                      "needs_basis": totals["needs_basis"]})
    return 2 if (totals["needs_basis"] or totals["no_price"] or totals["rejected"]) else 0



def cmd_consumption(args) -> int:
    """Материалы: потребность по нормам, списание, лимит закупки, давальческие."""
    from .consumption import build_report, load_json, load_table, write

    out_dir = Path(args.out)
    audit = AuditLog(out_dir / "audit.jsonl") if getattr(args, "audit", None) else None
    if bool(args.confirm_norms) != bool(args.by and args.role):
        if args.confirm_norms:
            print("   ✗ --confirm-norms требует --by и --role: норму принимает человек, "
                  "а не агент", file=sys.stderr)
            return 2

    works, problems = load_json(Path(args.works), "works")
    norms, norm_problems = load_json(Path(args.norms), "norms")
    problems += norm_problems
    writeoffs, p1 = (load_table(Path(args.writeoffs), "writeoff") if args.writeoffs else ([], []))
    purchases, p2 = (load_table(Path(args.purchases), "purchase") if args.purchases else ([], []))
    daval, p3 = (load_table(Path(args.daval), "daval") if args.daval else ([], []))
    problems += p1 + p2 + p3
    for problem in problems:
        print(f"   ⚠ {problem}")

    report = build_report(works, norms, writeoffs=writeoffs, purchases=purchases, daval=daval,
                          object_name=args.object_name or "", folder=getattr(args, "norms_dir", None),
                          confirmed_by=args.by or "", role=args.role or "",
                          confirm_norms=bool(args.confirm_norms))
    files = write(report, out_dir)
    totals = report["totals"]
    norms_total = report["norms"]

    print(f"материалы: {report['object_name'] or 'объект не указан'} · позиций {totals['materials']} · "
          f"норм применено {norms_total['applied']} из {norms_total['total']} "
          f"(без источника {norms_total['no_source']}, источник не подтверждён "
          f"{norms_total['unverified']})")
    print(f"  потребность: подтверждённая {totals['need_confirmed_rub']:,.2f} ₽ · "
          f"справочная {totals['need_reference_rub']:,.2f} ₽")
    if report["norm_confirmation"]:
        who = report["norm_confirmation"]
        print(f"  нормы приняты для расчёта человеком: {who['by']} ({who['role']})")
    for check in report["checks"]:
        mark = "✗" if check["level"] == "error" else "⚠"
        print(f"  {mark} {check['code']}: {check['message']}")
    print(f"  файлы: {files['json']}, {files['markdown']}, {files['xlsx']}")
    print("  отчёт считает потребность по нормам; закупку и списание решает человек")
    if audit is not None:
        audit.append("agent:materials", "agent", "materials.consumption_built",
                     f"object:{report['object_name'] or 'без объекта'}",
                     {"need_confirmed_rub": totals["need_confirmed_rub"],
                      "over_norms": totals["over_norms"], "over_limit": totals["over_limit"],
                      "errors": totals["errors"],
                      "norms_confirmed_by": (report["norm_confirmation"] or {}).get("by")})
    return 2 if totals["errors"] else 0



def cmd_checklist(args) -> int:
    """Чек-лист выполненных работ: пункты с источником, результат, подпись человека."""
    from .checklist import (RESULT_FAIL, RESULT_OK, RESULT_UNCHECKED, build_checklist, fill,
                            sign, to_markdown, write)

    out_dir = Path(args.out)
    audit = AuditLog(out_dir / "audit.jsonl") if getattr(args, "audit", None) else None
    action = args.checklist_action

    if action == "build":
        items = None
        if getattr(args, "items", None):
            from .consumption import load_json
            items, problems = load_json(Path(args.items), "items")
            for problem in problems:
                print(f"   ⚠ {problem}")
        checklist = build_checklist(work_type_id=args.work_type or "",
                                    object_name=args.object_name or "", number=args.number or "",
                                    act_ref=args.act_ref or "", items=items)
        files = write(checklist, out_dir)
        totals = checklist["totals"]
        print(f"чек-лист {checklist['number'] or 'б/н'}: {checklist['object_name'] or 'объект не указан'}")
        if checklist.get("work_type"):
            print(f"  вид работ: {checklist['work_type'].get('name') or checklist['work_type'].get('id')}")
        print(f"  пунктов: {totals['items']} · источников сверено {totals['source_verified']} · "
              f"отклонено пунктов без источника: {totals['rejected']}")
        for check in checklist["checks"]:
            mark = "✗" if check["level"] == "error" else "⚠"
            print(f"  {mark} {check['code']}: {check['message']}")
        print(f"  статус: {checklist['status']}")
        print(f"  файлы: {files['json']}, {files['docx']}")
        print("  работы принимает человек: подписывает только полностью проверенный чек-лист")
        return 2 if totals["rejected"] else 0

    path = Path(args.checklist)
    checklist = json.loads(path.read_text(encoding="utf-8"))

    if action == "fill":
        from .consumption import load_json
        results, problems = load_json(Path(args.results), "results")
        for problem in problems:
            print(f"   ⚠ {problem}")
        checklist = fill(checklist, results, by=args.by or "")
        for problem in checklist.get("problems") or []:
            print(f"   ⚠ {problem}")
        files = write(checklist, out_dir, stem=path.stem.replace("checklist_", ""))
        totals = checklist["totals"]
        print(f"заполнено: проверено {totals['checked']} из {totals['items']} · соответствует "
              f"{totals['ok']} · не соответствует {totals['failed']} · не проверено "
              f"{totals['unchecked']}")
        print(f"  статус: {checklist['status']}")
        print(f"  файлы: {files['json']}, {files['docx']}")
        bad = [c for c in checklist.get("checks") or [] if c["level"] == "error"]
        return 2 if (bad or checklist.get("problems")) else 0

    if action == "sign":
        checklist = sign(checklist, by=args.by, role=args.role, reason=args.reason or "",
                         audit_log=audit)
        if checklist.get("sign_problems"):
            print("   ПОДПИСЬ НЕ ПРИНЯТА:")
            for problem in checklist["sign_problems"]:
                print(f"     ✗ {problem}")
            return 2
        files = write(checklist, out_dir, stem=path.stem.replace("checklist_", ""))
        print(f"подписал: {args.by} ({args.role}) · статус: {checklist['status']}")
        close = checklist["can_close"]
        if close["allowed"]:
            print("  работы по чек-листу можно предъявлять: все пункты проверены, "
                  "несоответствий нет")
        else:
            print("  работы предъявлять нельзя:")
            for reason in close["reasons"]:
                print(f"     · {reason}")
        print(f"  файлы: {files['json']}, {files['docx']}")
        return 0

    print("неизвестное действие", file=sys.stderr)
    return 2



def cmd_journal(args) -> int:
    """Журналы: общий журнал работ и журнал верификации закупленной продукции."""
    from .cumulative import load_acts
    from .journals import (build_verification_journal, build_work_journal, load_json, sign,
                           to_markdown, write)

    out_dir = Path(args.out)
    audit = AuditLog(out_dir / "audit.jsonl") if getattr(args, "audit", None) else None
    action = args.journal_action

    if action == "sign":
        path = Path(args.journal)
        journal = json.loads(path.read_text(encoding="utf-8"))
        journal = sign(journal, by=args.by, role=args.role, reason=args.reason or "",
                       audit_log=audit)
        if journal.get("sign_problems"):
            print("   ПОДПИСЬ НЕ ПРИНЯТА:")
            for problem in journal["sign_problems"]:
                print(f"     ✗ {problem}")
            return 2
        files = write(journal, out_dir, stem=path.stem)
        print(f"подписал: {args.by} ({args.role}) · статус: {journal['status']}")
        close = journal["can_close"]
        print("  журнал можно закрывать" if close["allowed"] else
              "  закрывать нельзя: " + "; ".join(close["reasons"]))
        print(f"  файлы: {files['json']}, {files['xlsx']}")
        return 0

    acts = None
    if getattr(args, "acts", None):
        acts_data, problems = load_acts(Path(args.acts))
        for problem in problems:
            print(f"   ⚠ {problem}")
        acts = acts_data or None

    if action == "work":
        entries, problems = load_json(Path(args.entries), "entries")
        for problem in problems:
            print(f"   ⚠ {problem}")
        journal = build_work_journal(entries, object_name=args.object_name or "",
                                     number=args.number or "", acts=acts)
    else:
        records, problems = load_json(Path(args.records), "records")
        for problem in problems:
            print(f"   ⚠ {problem}")
        writeoffs = []
        if getattr(args, "writeoffs", None):
            from .consumption import load_table
            writeoffs, more = load_table(Path(args.writeoffs), "writeoff")
            for problem in more:
                print(f"   ⚠ {problem}")
        journal = build_verification_journal(records, object_name=args.object_name or "",
                                             number=args.number or "", writeoffs=writeoffs)

    files = write(journal, out_dir)
    totals = journal["totals"]
    print(f"{journal['title']}: {journal['object_name'] or 'объект не указан'}")
    if action == "work":
        print(f"  записей: {totals['entries']} · смен: {totals['shifts']} · видов работ: "
              f"{totals['work_types']} · с погодой: {totals['with_weather']} · отклонено: "
              f"{totals['rejected']}")
    else:
        print(f"  проверок партий: {totals['records']} · допущено: {totals['accepted']} · "
              f"без документа качества: {totals['without_doc']} · списано без контроля: "
              f"{totals['unverified_writeoffs']}")
    for check in journal["checks"]:
        mark = "✗" if check["level"] == "error" else "⚠"
        print(f"  {mark} {check['code']}: {check['message']}")
    close = journal["can_close"]
    print("  статус: " + journal["status"])
    if not close["allowed"]:
        for reason in close["reasons"]:
            print(f"     · {reason}")
    print(f"  файлы: {files['json']}, {files['markdown']}, {files['xlsx']}")
    print("  журнал ведёт и подписывает человек: агент проверяет полноту, хронологию и допуск")
    return 2 if totals["errors"] else 0



def cmd_quality(args) -> int:
    """Документы качества на материалы: реестр, привязка к партиям, опись и подпись."""
    from .journals import load_json
    from .quality_docs import (build_registry, load_json as load_list, sign, to_markdown, write)

    out_dir = Path(args.out)
    audit = AuditLog(out_dir / "audit.jsonl") if getattr(args, "audit", None) else None
    action = args.quality_action

    if action == "sign":
        path = Path(args.registry)
        registry = json.loads(path.read_text(encoding="utf-8"))
        registry = sign(registry, by=args.by, role=args.role, reason=args.reason or "",
                        audit_log=audit)
        if registry.get("sign_problems"):
            print("   ПОДПИСЬ НЕ ПРИНЯТА:")
            for problem in registry["sign_problems"]:
                print(f"     ✗ {problem}")
            return 2
        files = write(registry, out_dir, stem=path.stem)
        print(f"подписал: {args.by} ({args.role}) · статус: {registry['status']}")
        close = registry["can_transfer"]
        print("  комплект можно передавать" if close["allowed"] else
              "  передавать нельзя: " + "; ".join(close["reasons"]))
        print(f"  файлы: {files['json']}, {files['docx']}")
        return 0

    documents, problems = load_list(Path(args.documents), "documents")
    requirements: list[dict] = []
    if getattr(args, "requirements", None):
        requirements, more = load_list(Path(args.requirements), "requirements")
        problems += more
    usage: list[dict] = []
    if getattr(args, "usage", None):
        usage, more = load_list(Path(args.usage), "usage")
        problems += more
    verification: list[dict] = []
    if getattr(args, "verification", None):
        verification, more = load_json(Path(args.verification), "records")
        problems += more
    for problem in problems:
        print(f"   ⚠ {problem}")

    registry = build_registry(documents, object_name=args.object_name or "",
                              number=args.number or "", requirements=requirements,
                              usage=usage, verification=verification)
    files = write(registry, out_dir)
    totals = registry["totals"]
    print(f"{registry['title']}: {registry['object_name'] or 'объект не указан'}")
    print(f"  документов: {totals['documents']} · отклонено: {totals['rejected']} · "
          f"материалов: {totals['materials']} · партий с документом: "
          f"{totals['covered_batches']} из {totals['batches']}")
    for card in registry["materials"]:
        print(f"  {card['material']}: {card['status']}"
              + (f" · не хватает: {', '.join(card['missing'])}" if card["missing"] else ""))
    for check in registry["checks"]:
        mark = "✗" if check["level"] == "error" else "⚠"
        print(f"  {mark} {check['code']}: {check['message']}")
    print("  статус: " + registry["status"])
    for reason in registry["can_transfer"]["reasons"]:
        print(f"     · {reason}")
    print(f"  файлы: {files['json']}, {files['markdown']}, {files['xlsx']}, {files['docx']}")
    print("  комплект закрывает человек: агент проверяет полноту, партии и сроки")
    return 2 if totals["errors"] else 0



def cmd_docflow(args) -> int:
    """РД «В производство работ» и согласование ИД до подписи."""
    from .docflow import (build_rd_registry, build_submittal_registry, load_list, sign,
                          to_markdown, write)

    out_dir = Path(args.out)
    audit = AuditLog(out_dir / "audit.jsonl") if getattr(args, "audit", None) else None
    action = args.docflow_action

    if action == "sign":
        path = Path(args.registry)
        document = json.loads(path.read_text(encoding="utf-8"))
        document = sign(document, by=args.by, role=args.role, decision=args.decision or "",
                        reason=args.reason or "", audit_log=audit)
        if document.get("sign_problems"):
            print("   ПОДПИСЬ НЕ ПРИНЯТА:")
            for problem in document["sign_problems"]:
                print(f"     ✗ {problem}")
            return 2
        files = write(document, out_dir, stem=path.stem)
        print(f"подписал: {args.by} ({args.role}) · статус: {document['status']}")
        close = document.get("can_work") or document.get("can_close") or {}
        print("  закрыто" if close.get("allowed") else
              "  закрывать нельзя: " + "; ".join(close.get("reasons") or []))
        print(f"  файлы: {files['json']}, {files['xlsx']}")
        return 0

    if action == "rd":
        transmittals, problems = load_list(Path(args.transmittals), "transmittals")
        revisions: list[dict] = []
        if getattr(args, "revisions", None):
            revisions, more = load_list(Path(args.revisions), "revisions")
            problems += more
        works: list[dict] = []
        if getattr(args, "works", None):
            works, more = load_list(Path(args.works), "works")
            problems += more
        for problem in problems:
            print(f"   ⚠ {problem}")
        document = build_rd_registry(transmittals, revisions=revisions, works=works,
                                     object_name=args.object_name or "", number=args.number or "")
    else:
        submittals, problems = load_list(Path(args.submittals), "submittals")
        for problem in problems:
            print(f"   ⚠ {problem}")
        document = build_submittal_registry(submittals, object_name=args.object_name or "",
                                            number=args.number or "")

    files = write(document, out_dir)
    totals = document["totals"]
    print(f"{document['title']}: {document['object_name'] or 'объект не указан'}")
    if action == "rd":
        print(f"  передач: {totals['transmittals']} · отклонено: {totals['rejected']} · "
              f"документов: {totals['documents']} · со штампом: {totals['with_stamp']}")
        for doc in document["documents"]:
            print(f"  {doc['doc']}: {doc['status']} (действующая редакция "
                  f"{doc.get('current_revision') or '—'})")
    else:
        print(f"  комплектов: {totals['submittals']} · подписано: {totals['signed']} · "
              f"с открытыми замечаниями: {totals['with_open_remarks']} · замечаний: "
              f"{totals['remarks']} (просрочено: {totals['overdue']})")
    for check in document["checks"]:
        mark = "✗" if check["level"] == "error" else "⚠"
        print(f"  {mark} {check['code']}: {check['message']}")
    print("  статус: " + document["status"])
    close = document.get("can_work") or document.get("can_close") or {}
    for reason in close.get("reasons") or []:
        print(f"     · {reason}")
    print(f"  файлы: {files['json']}, {files['markdown']}, {files['xlsx']}")
    print("  решение принимает человек: агент сверяет редакции, штампы, сроки и подписи")
    return 2 if totals["errors"] else 0


def cmd_correspondence(args) -> int:
    """Письма и запросы, лаборатории и геодезисты, авторские дни и дни качества."""
    from .correspondence import (build_involvement_registry, build_letter_registry,
                                 build_quality_days, load_list, sign, to_markdown, write)

    out_dir = Path(args.out)
    audit = AuditLog(out_dir / "audit.jsonl") if getattr(args, "audit", None) else None
    action = args.corr_action

    if action == "sign":
        path = Path(args.registry)
        document = json.loads(path.read_text(encoding="utf-8"))
        document = sign(document, by=args.by, role=args.role, reason=args.reason or "",
                        audit_log=audit)
        if document.get("sign_problems"):
            print("   ПОДПИСЬ НЕ ПРИНЯТА:")
            for problem in document["sign_problems"]:
                print(f"     ✗ {problem}")
            return 2
        files = write(document, out_dir, stem=path.stem)
        print(f"подписал: {args.by} ({args.role}) · статус: {document['status']}")
        print(f"  файлы: {files['json']}, {files['xlsx']}")
        return 0

    key = {"letters": "letters", "involvement": "records", "days": "events"}[action]
    items, problems = load_list(Path(args.source), key)
    for problem in problems:
        print(f"   ⚠ {problem}")
    builder = {"letters": build_letter_registry, "involvement": build_involvement_registry,
               "days": build_quality_days}[action]
    document = builder(items, object_name=args.object_name or "", number=args.number or "")
    files = write(document, out_dir)
    totals = document["totals"]
    print(f"{document['title']}: {document['object_name'] or 'объект не указан'}")
    if action == "letters":
        print(f"  писем: {totals['letters']} · требуют ответа: {totals['require_answer']} · "
              f"ответов: {totals['answered']} · ждём: {totals['awaiting']}")
    elif action == "involvement":
        print(f"  заявок: {totals['records']} · лабораторий: {totals['laboratories']} · "
              f"геодезистов: {totals['geodesists']} · с результатом: {totals['with_result']}")
    else:
        print(f"  мероприятий: {totals['events']} · замечаний: {totals['remarks']} · "
              f"открыто: {totals['open_remarks']} · просрочено: {totals['overdue']}")
    for check in document["checks"]:
        mark = "✗" if check["level"] == "error" else "⚠"
        print(f"  {mark} {check['code']}: {check['message']}")
    print("  статус: " + document["status"])
    for reason in (document.get("can_close") or {}).get("reasons") or []:
        print(f"     · {reason}")
    print(f"  файлы: {files['json']}, {files['markdown']}, {files['xlsx']}")
    return 2 if totals["errors"] else 0



def cmd_pack(args) -> int:
    """Комплект объекта: все разделы одной командой и один вердикт «можно ли предъявлять»."""
    from .pack import build_pack, write

    out_dir = Path(args.out)
    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    base = Path(args.base_dir or ".")
    pack = build_pack(config, base_dir=base, out_dir=out_dir)
    files = pack["files"]
    totals = pack["totals"]
    print(f"{pack['title']}: {pack['object_name'] or 'объект не указан'} · "
          f"комплект {pack['number'] or 'б/н'}")
    print(f"  разделов: {totals['sections']} · без ошибок: {totals['ok']} · с ошибками: "
          f"{totals['with_errors']} · нет данных: {totals['missing']} · исключено: "
          f"{totals['disabled']} · подписано: {totals['signed']}")
    for section in pack["sections"]:
        mark = {"ошибок нет": "✓", "есть предупреждения": "⚠", "есть ошибки": "✗",
                "нет данных": "·", "исключён человеком": "–"}.get(section["status"], "?")
        tail = f" · {section['why'][:60]}" if section.get("why") else ""
        print(f"  {mark} {section['title'][:46]:46} {section['status']}"
              f"{' · подписан' if section['signed'] else ''}{tail}")
    close = pack["can_submit"]
    if close["allowed"]:
        print("  КОМПЛЕКТ МОЖНО ПРЕДЪЯВЛЯТЬ")
    else:
        print("  предъявлять нельзя:")
        for reason in close["reasons"]:
            print(f"     · {reason}")
    unsigned = [s["title"] for s in pack["sections"] if not s["signed"]
                and s["status"] not in ("исключён человеком", "нет данных")]
    if unsigned:
        print("  подписать (ФИО, роль, дата): " + ", ".join(unsigned))
    print(f"  файлы: {files['json']}, {files['markdown']}, {files['xlsx']}, {files['docx']}")
    print(f"  разделы лежат в: {out_dir}")
    print("  вердикт считает агент, решение принимает ИТР: подписи ставит человек")
    return 0 if close["allowed"] else 2


def cmd_pack_portfolio(args) -> int:
    """Портфель комплектов: где можно предъявлять и сколько денег это держит."""
    from .pack_portfolio import build_portfolio

    def money(value) -> str:
        return "не оценено" if value is None else f"{value:,.2f} ₽".replace(",", " ")

    out_dir = Path(args.out)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    base = Path(args.base_dir or ".")
    portfolio = build_portfolio(config, base_dir=base, out_dir=out_dir)
    files = portfolio["files"]
    totals, money_totals = portfolio["totals"], portfolio["money"]
    print(f"{portfolio['title']}: {portfolio['name']} · на {portfolio['built_at']}")
    print(f"  объектов: {totals['objects']} · можно предъявлять: {totals['allowed']} · "
          f"с блокерами: {totals['blocked']} · без комплекта: {totals['without_pack']}")
    for obj in portfolio["objects"]:
        mark = "✓" if obj["can_submit"] else ("✗" if obj["pack_built"] else "·")
        close = ("можно предъявлять" if obj["can_submit"]
                 else "предъявлять нельзя" if obj["pack_built"] else "комплекта нет")
        print(f"  {mark} {obj['object_name'][:34]:34} {obj['pack_number'] or 'б/н':>6} "
              f"{close:18} под риском: {money(obj['at_risk_rub']):>16} · {obj['fix']}")
        for act in obj["acts"]:
            amount = "нет суммы в акте" if act["amount_rub"] is None else money(act["amount_rub"])
            print(f"      {act['act_id']:14} {act['period'] or '—':16} {amount:>18}"
                  f"  {act['status']}")
    print(f"  актов: {totals['acts']} · под риском непредъявления: {totals['acts_at_risk']} · "
          f"не оценено: {money_totals['unestimated_acts']}")
    print(f"  ПОД РИСКОМ НЕПРЕДЪЯВЛЕНИЯ: {money(money_totals['at_risk_rub'])} · "
          f"предъявлено: {money(money_totals['released_rub'])}")
    for row in money_totals["by_fix"]:
        print(f"     · {row['fix']}: {money(row['rub'])} по {row['acts']} актам")
    for act in money_totals["unestimated"]:
        print(f"     · не оценено: {act['act_id']} — {act['why']}")
    blockers = [c for c in portfolio["checks"] if c["level"] == "error"]
    for check in blockers:
        print(f"  ✗ {check['code']}: {check['message'][:120]}")
    print("  вердикт: " + portfolio["verdict"]["summary"])
    print(f"  файлы: {files['json']}, {files['markdown']}, {files['xlsx']}, {files['docx']}")
    print("  вердикт считает агент, решение о предъявлении принимает ИТР: подписи ставит человек")
    return 0 if portfolio["verdict"]["any_releasable"] and not blockers else 2


def cmd_funding(args) -> int:
    """График финансирования: что попадёт в платёж, что переносится и по какой цене."""
    from .funding import build_plan

    def money(value) -> str:
        return "не оценено" if value is None else f"{value:,.2f} ₽".replace(",", " ")

    out_dir = Path(args.out)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    base = Path(args.base_dir or ".")
    today = (date.fromisoformat(args.today) if getattr(args, "today", None) else None)
    plan = build_plan(config, base_dir=base, out_dir=out_dir, today=today)
    files = plan["files"]
    totals = plan["totals"]
    contract = plan["contract"]
    print(f"{plan['title']}: {plan['name']} · договор {contract.get('number') or 'не указан'} "
          f"· на {plan['built_at']}")
    print(f"  строк графика: {totals['payments']} · запланировано: {money(totals['plan_rub'])}")
    print(f"  В ПЛАТЁЖ: {money(totals['in_payment_rub'])} · переносится: "
          f"{money(totals['transfer_rub'])} · пени: {money(totals['penalty_rub'])} · "
          f"удержание: {money(totals['retention_rub'])}")
    for month in plan["months"]:
        print(f"  {month['label']:16} строк {month['payments']} · в платёж "
              f"{money(month['in_payment_rub']):>16} · перенос {money(month['transfer_rub']):>16}"
              f" · пени {money(month['penalty_rub']):>12} · пропущено сроков {month['missed']}")
    for row in plan["payments"]:
        mark = {"в платёж": "✓", "перенос": "✗", "не оценено": "·",
                "не сверено с актом": "?"}.get(row["status"], "?")
        deadline = row["submission_deadline"] or "срок не заявлен"
        if row.get("days_overdue"):
            deadline += f" (просрочен {row['days_overdue']} дн.)"
        elif row.get("days_to_deadline") is not None:
            deadline += f" (осталось {row['days_to_deadline']} дн.)"
        print(f"  {mark} {row['month_label']:15} {row['act_id']:12} "
              f"{money(row['amount_rub']):>16}  {deadline:34} {row['status']}")
    for check in plan["checks"]:
        if check["level"] != "error":
            continue
        print(f"  ✗ {check['code']}: {check['message'][:130]}")
    warnings = [c for c in plan["checks"] if c["level"] == "warning"]
    for check in warnings:
        print(f"  ⚠ {check['code']}: {check['message'][:130]}")
    print("  вердикт: " + plan["verdict"]["summary"])
    print(f"  файлы: {files['json']}, {files['markdown']}, {files['xlsx']}, {files['docx']}")
    print("  сроки и ставки — из графика и договора; чего в них нет, то «не заявлено», а не 0 ₽")
    return 0 if not totals["errors"] else 2


def cmd_monthly(args) -> int:
    """Месячный план: работы месяца, нужные документы, что готово к предъявлению."""
    from .monthly import build_plan

    def money(value) -> str:
        return "не оценено" if value is None else f"{value:,.2f} ₽".replace(",", " ")

    out_dir = Path(args.out)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    base = Path(args.base_dir or ".")
    today = (date.fromisoformat(args.today) if getattr(args, "today", None) else None)
    plan = build_plan(config, base_dir=base, out_dir=out_dir, today=today)
    files = plan["files"]
    totals = plan["totals"]
    print(f"{plan['title']}: {plan['name']} · на {plan['built_at']}")
    if plan.get("matrix"):
        profile = plan["matrix"]["profile"]
        print(f"  требования: матрица «{profile.get('name')}» v{profile.get('version')}, "
              f"{plan['matrix']['work_types']} видов работ")
    print(f"  актов: {totals['acts']} · видов работ: {totals['works']} · готово: "
          f"{totals['ready']} · не готово: {totals['not_ready']} · без требований в матрице: "
          f"{totals['no_requirements']}")
    print(f"  документов нужно: {totals['docs_required']} · не хватает: "
          f"{totals['docs_missing']} · без подписи: {totals['docs_unsigned']}")
    print(f"  ГОТОВО К ПРЕДЪЯВЛЕНИЮ: {money(totals['money_ready_rub'])} · держит документ: "
          f"{money(totals['money_waiting_rub'])}")
    for act in plan["acts"]:
        deadline = act["submission_deadline"] or "срок не заявлен"
        if act.get("days_to_deadline") is not None:
            days = act["days_to_deadline"]
            deadline += f" (просрочен {-days} дн.)" if days < 0 else f" (осталось {days} дн.)"
        print(f"  {act['act_id']} · {act.get('object') or 'объект не указан'} · "
              f"{money(act['amount_rub'])} · срок: {deadline}")
        for work in act["works"]:
            mark = {"готово к предъявлению": "✓", "не готово": "✗",
                    "требований в матрице нет": "–"}.get(work["status"], "?")
            gaps = len(work["missing"]) + len(work["unsigned"])
            print(f"    {mark} {work['name'][:44]:46} {money(work['amount_rub']):>16}  "
                  f"{work['status']}" + (f" · не хватает {gaps}" if gaps else ""))
    if plan["actions"]:
        print("  что сделать в первую очередь:")
        for action in plan["actions"][:8]:
            print(f"     · {action['what']}: {action['document'][:58]} — {action['work'][:34]} "
                  f"({money(action['rub'])})")
    blockers = [c for c in plan["checks"] if c["level"] == "error"]
    for check in blockers[:12]:
        print(f"  ✗ {check['code']}: {check['message'][:120]}")
    if len(blockers) > 12:
        print(f"  … ещё блокеров: {len(blockers) - 12} (полный список — в monthly_*.json)")
    for check in [c for c in plan["checks"] if c["level"] == "warning"]:
        print(f"  ⚠ {check['code']}: {check['message'][:120]}")
    print("  вердикт: " + plan["verdict"]["summary"])
    print(f"  файлы: {files['json']}, {files['markdown']}, {files['xlsx']}, {files['docx']}")
    print("  требования — из матрицы, наличие — из переданных документов; подписи ставит человек")
    return 0 if plan["verdict"]["ready_to_invoice"] else 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # подкоманды CAD и MCP живут в своих пакетах: делегируем до разбора argparse,
    # чтобы их собственные ключи (--transport, --mode и т.п.) не перехватывались
    if argv and argv[0] == "cad":
        from .cad.cli import main as cad_main
        return cad_main(argv[1:])
    if argv and argv[0] == "asbuilt":
        from .asbuilt.cli import main as asbuilt_main
        return asbuilt_main(argv[1:])
    if argv and argv[0] == "mcp":
        from .cad.mcp_server import main as mcp_main
        return mcp_main(argv[1:])

    p = argparse.ArgumentParser(prog="pto_agent", description="ИИ-агент ПТО (MVP)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="полный цикл: сверка → КС-2 → отчёты")
    r.add_argument("--data", default="data/demo")
    r.add_argument("--out", default="out")
    r.add_argument("--object-name", default="ЖК «Северный», корпус 2")
    r.add_argument("--vat", type=float, default=20.0)
    r.add_argument("--norm-query", default="минимальная толщина защитного слоя бетона для фундамента")
    r.add_argument("--use-llm", action="store_true", help="переформулировать резюме через LLM (если настроен провайдер)")
    r.set_defaults(func=cmd_run)

    n = sub.add_parser("norms", help="нормативный запрос")
    n.add_argument("query")
    n.add_argument("--data", default="data/demo")
    n.add_argument("--as-of", default=None, help="дата применимости норм, YYYY-MM-DD")
    n.set_defaults(func=cmd_norms)

    pr = sub.add_parser("prices", help="цены ресурсов: импорт выгрузки, поиск, история версий")
    pr.add_argument("--out", default="data/prices")
    pr.add_argument("--audit", default=None, help="журнал аудита (по умолчанию не пишем)")
    prsub = pr.add_subparsers(dest="prices_action", required=True)
    pi = prsub.add_parser("import", help="импортировать выгрузку цен как новую версию")
    pi.add_argument("--file", required=True)
    pi.add_argument("--source", required=True, help="ФГИС ЦС, письмо индексов, прайс поставщика")
    pi.add_argument("--date", required=True, help="дата, на которую действуют цены, YYYY-MM-DD")
    pi.add_argument("--region", default="")
    pi.add_argument("--zone", default="")
    pi.add_argument("--actor", default="agent:prices")
    pi.set_defaults(func=cmd_prices)
    pl = prsub.add_parser("lookup", help="найти цену по коду/наименованию на дату")
    pl.add_argument("--code", default="")
    pl.add_argument("--name", default="")
    pl.add_argument("--as-of", default=None)
    pl.set_defaults(func=cmd_prices)
    ph = prsub.add_parser("history", help="история цены по версиям")
    ph.add_argument("--code", default="")
    ph.add_argument("--name", default="")
    ph.set_defaults(func=cmd_prices)
    pls = prsub.add_parser("list", help="список версий цен")
    pls.set_defaults(func=cmd_prices)

    pc = sub.add_parser("procure", help="закупка материалов: заявка из сметы, сравнение КП, решение")
    pc.add_argument("--out", default="out/procurement")
    pc.add_argument("--audit", default=None, help="журнал аудита для решений")
    pcsub = pc.add_subparsers(dest="procure_action", required=True)
    prq = pcsub.add_parser("request", help="заявка на материалы из сметы")
    prq.add_argument("--smeta", required=True)
    prq.add_argument("--object-name", default="")
    prq.add_argument("--request-no", default="")
    prq.add_argument("--need-by", default="")
    prq.set_defaults(func=cmd_procure)
    prc = pcsub.add_parser("compare", help="сравнить КП по заявке")
    prc.add_argument("--request", required=True)
    prc.add_argument("--quotes", nargs="+", required=True)
    prc.add_argument("--prefer", default="price", choices=["price", "delivery"])
    prc.set_defaults(func=cmd_procure)
    prd = pcsub.add_parser("decide", help="зафиксировать выбор человека (в журнал аудита)")
    prd.add_argument("--request", required=True)
    prd.add_argument("--quotes", nargs="+", required=True)
    prd.add_argument("--choose", nargs="+", default=[], help="код=поставщик (можно несколько)")
    prd.add_argument("--by", required=True)
    prd.add_argument("--role", default="инженер ПТО")
    prd.add_argument("--reason", default="")
    prd.add_argument("--prefer", default="price", choices=["price", "delivery"])
    prd.set_defaults(func=cmd_procure)

    mt = sub.add_parser("materials", help="материалы: сверка «заявка → приход → КС-2»")
    mt.add_argument("--out", default="out/materials")
    mtsub = mt.add_subparsers(dest="materials_action", required=True)
    mtr = mtsub.add_parser("reconcile", help="сверить решение по поставщику, накладные и акт")
    mtr.add_argument("--decision", required=True, help="decision.json из procure decide")
    mtr.add_argument("--receipts", nargs="*", default=[], help="накладные/УПД (csv, xlsx)")
    mtr.add_argument("--act", nargs="*", default=[], help="материалы КС-2 построчно (csv, xlsx)")
    mtr.add_argument("--object-name", default="")
    mtr.set_defaults(func=cmd_materials)

    pk = sub.add_parser("pack",
                        help="комплект объекта: все разделы одной командой и один вердикт")
    pk.add_argument("--out", default="out/pack")
    pk.add_argument("--base-dir", default=".")
    pksub = pk.add_subparsers(dest="pack_action", required=True)
    pkb = pksub.add_parser("build", help="собрать комплект по конфигу")
    pkb.add_argument("--config", required=True, help="JSON конфига комплекта")
    pkb.set_defaults(func=cmd_pack)
    pkp = pksub.add_parser("portfolio", help="портфель комплектов: где можно предъявлять и "
                                             "сколько денег это держит")
    pkp.add_argument("--config", required=True, help="JSON портфеля: объекты, комплекты, акты")
    pkp.set_defaults(func=cmd_pack_portfolio)

    fin = sub.add_parser("finance",
                         help="график финансирования: что попадёт в платёж, что переносится")
    fin.add_argument("--out", default="out/finance")
    fin.add_argument("--base-dir", default=".")
    finsub = fin.add_subparsers(dest="finance_action", required=True)
    finp = finsub.add_parser("plan", help="свести график платежей с портфелем комплектов")
    finp.add_argument("--config", required=True, help="JSON: график, договор, портфель")
    finp.add_argument("--today", default=None,
                      help="дата расчёта YYYY-MM-DD (по умолчанию сегодня): "
                           "сроки и пени считаются на неё, а не на дату запуска")
    finp.set_defaults(func=cmd_funding)

    mon = sub.add_parser("monthly",
                         help="месячный план: работы, нужные документы, что готово к предъявлению")
    mon.add_argument("--out", default="out/monthly")
    mon.add_argument("--base-dir", default=".")
    monsub = mon.add_subparsers(dest="monthly_action", required=True)
    monp = monsub.add_parser("plan", help="собрать план месяца по актам и документам")
    monp.add_argument("--config", required=True, help="JSON: месяц, акты, позиции, документы")
    monp.set_defaults(func=cmd_monthly)

    dfl = sub.add_parser("docflow",
                         help="РД «В производство работ» и согласование ИД до подписи")
    dfl.add_argument("--out", default="out/docflow")
    dfl.add_argument("--audit", default=None)
    dflsub = dfl.add_subparsers(dest="docflow_action", required=True)
    dfr = dflsub.add_parser("rd", help="реестр выдачи рабочей документации")
    dfr.add_argument("--transmittals", required=True, help="JSON передач РД")
    dfr.add_argument("--revisions", default=None, help="JSON реестра редакций РД")
    dfr.add_argument("--works", default=None, help="JSON работ со ссылкой на РД")
    dfr.add_argument("--object-name", default="")
    dfr.add_argument("--number", default="")
    dfr.set_defaults(func=cmd_docflow)
    dfs = dflsub.add_parser("submittals", help="реестр согласования ИД: замечания и подпись")
    dfs.add_argument("--submittals", required=True, help="JSON комплектов ИД")
    dfs.add_argument("--object-name", default="")
    dfs.add_argument("--number", default="")
    dfs.set_defaults(func=cmd_docflow)
    dfg = dflsub.add_parser("sign", help="подписать реестр (ФИО и роль обязательны)")
    dfg.add_argument("--registry", required=True)
    dfg.add_argument("--by", required=True)
    dfg.add_argument("--role", required=True)
    dfg.add_argument("--decision", default="")
    dfg.add_argument("--reason", default="")
    dfg.set_defaults(func=cmd_docflow)

    cor = sub.add_parser("correspondence",
                         help="письма и запросы, лаборатории и геодезисты, дни качества")
    cor.add_argument("--out", default="out/correspondence")
    cor.add_argument("--audit", default=None)
    corsub = cor.add_subparsers(dest="corr_action", required=True)
    for name, helptext in (("letters", "реестр писем и запросов"),
                           ("involvement", "реестр лабораторий и геодезистов"),
                           ("days", "авторские дни и дни качества")):
        sub_parser = corsub.add_parser(name, help=helptext)
        sub_parser.add_argument("--source", required=True, help="JSON записей")
        sub_parser.add_argument("--object-name", default="")
        sub_parser.add_argument("--number", default="")
        sub_parser.set_defaults(func=cmd_correspondence)
    cors = corsub.add_parser("sign", help="подписать реестр (ФИО и роль обязательны)")
    cors.add_argument("--registry", required=True)
    cors.add_argument("--by", required=True)
    cors.add_argument("--role", required=True)
    cors.add_argument("--reason", default="")
    cors.set_defaults(func=cmd_correspondence)

    qua = sub.add_parser("quality",
                         help="документы качества на материалы: реестр, привязка к партиям, опись")
    qua.add_argument("--out", default="out/quality")
    qua.add_argument("--audit", default=None)
    quasub = qua.add_subparsers(dest="quality_action", required=True)
    quab = quasub.add_parser("build", help="собрать реестр документов качества")
    quab.add_argument("--documents", required=True, help="JSON документов качества")
    quab.add_argument("--requirements", default=None,
                      help="JSON требований к составу документов по материалам")
    quab.add_argument("--usage", default=None, help="JSON применения материалов по партиям")
    quab.add_argument("--verification", default=None,
                      help="журнал входного контроля (JSON) для стыка с кругом 7")
    quab.add_argument("--object-name", default="")
    quab.add_argument("--number", default="")
    quab.set_defaults(func=cmd_quality)
    quas = quasub.add_parser("sign", help="подписать комплект (ФИО и роль обязательны)")
    quas.add_argument("--registry", required=True)
    quas.add_argument("--by", required=True)
    quas.add_argument("--role", required=True)
    quas.add_argument("--reason", default="")
    quas.set_defaults(func=cmd_quality)

    jrn = sub.add_parser("journal",
                         help="общий журнал работ и журнал верификации закупленной продукции")
    jrn.add_argument("--out", default="out/journals")
    jrn.add_argument("--audit", default=None)
    jrnsub = jrn.add_subparsers(dest="journal_action", required=True)
    jrnw = jrnsub.add_parser("work", help="общий журнал работ по записям смен")
    jrnw.add_argument("--entries", required=True, help="JSON записей журнала")
    jrnw.add_argument("--object-name", default="")
    jrnw.add_argument("--number", default="")
    jrnw.add_argument("--acts", default=None, help="реестр актов КС-2 для сверки объёмов")
    jrnw.set_defaults(func=cmd_journal)
    jrnv = jrnsub.add_parser("verification",
                             help="журнал верификации закупленной продукции (входной контроль)")
    jrnv.add_argument("--records", required=True, help="JSON записей входного контроля")
    jrnv.add_argument("--object-name", default="")
    jrnv.add_argument("--number", default="")
    jrnv.add_argument("--writeoffs", default=None,
                      help="списание (xlsx, csv): что применено без входного контроля")
    jrnv.set_defaults(func=cmd_journal)
    jrns = jrnsub.add_parser("sign", help="подписать журнал (ФИО и роль обязательны)")
    jrns.add_argument("--journal", required=True)
    jrns.add_argument("--by", required=True)
    jrns.add_argument("--role", required=True)
    jrns.add_argument("--reason", default="")
    jrns.set_defaults(func=cmd_journal)

    chk = sub.add_parser("checklist",
                         help="чек-лист выполненных работ: пункты с источником, результат, подпись")
    chk.add_argument("--out", default="out/checklists")
    chk.add_argument("--audit", default=None)
    chksub = chk.add_subparsers(dest="checklist_action", required=True)
    chkb = chksub.add_parser("build", help="собрать чек-лист по виду работ")
    chkb.add_argument("--work-type", default="", help="id вида работ из матрицы")
    chkb.add_argument("--object-name", default="")
    chkb.add_argument("--number", default="")
    chkb.add_argument("--act-ref", default="", help="акт КС-2, к которому относится чек-лист")
    chkb.add_argument("--items", default=None,
                      help="JSON пунктов организации: [{requirement, control, source{doc,point,edition}}]")
    chkb.set_defaults(func=cmd_checklist)
    chkf = chksub.add_parser("fill", help="внести результаты по пунктам")
    chkf.add_argument("--checklist", required=True, help="JSON чек-листа")
    chkf.add_argument("--results", required=True,
                      help="JSON результатов: [{no, result, note, measured}]")
    chkf.add_argument("--by", default="", help="кто заполнил")
    chkf.set_defaults(func=cmd_checklist)
    chks = chksub.add_parser("sign", help="подписать чек-лист (ФИО и роль обязательны)")
    chks.add_argument("--checklist", required=True)
    chks.add_argument("--by", required=True)
    chks.add_argument("--role", required=True)
    chks.add_argument("--reason", default="")
    chks.set_defaults(func=cmd_checklist)

    cons = sub.add_parser("consumption",
                          help="материалы: потребность по нормам, списание, лимит, давальческие")
    cons.add_argument("--works", required=True, help="ведомость объёмов работ (JSON)")
    cons.add_argument("--norms", required=True, help="нормы расхода (JSON, с источником)")
    cons.add_argument("--writeoffs", default=None, help="списание по актам (xlsx, csv)")
    cons.add_argument("--purchases", default=None, help="закупки: лимит против факта (xlsx, csv)")
    cons.add_argument("--daval", default=None, help="давальческие материалы (xlsx, csv)")
    cons.add_argument("--object-name", default="")
    cons.add_argument("--norms-dir", default=None, help="каталог нормативной базы для сверки")
    cons.add_argument("--confirm-norms", action="store_true",
                      help="человек принял нормы для расчёта (нужны --by и --role)")
    cons.add_argument("--by", default="", help="ФИО того, кто принял нормы")
    cons.add_argument("--role", default="", help="должность того, кто принял нормы")
    cons.add_argument("--out", default="out/consumption")
    cons.add_argument("--audit", default=None)
    cons.set_defaults(func=cmd_consumption)

    cum = sub.add_parser("cumulative",
                         help="накопительная ведомость выполненных работ (нарастающим итогом)")
    cum.add_argument("--acts", required=True, help="реестр актов объекта (acts.json)")
    cum.add_argument("--estimate", default=None, help="смета (xlsx): остаток и цены по коду")
    cum.add_argument("--out", default="out/cumulative")
    cum.add_argument("--strict", action="store_true",
                     help="код 2 и при предупреждениях, не только при ошибках")
    cum.set_defaults(func=cmd_cumulative)

    atn = sub.add_parser("atn", help="акт на вновь возникающие и дополнительные работы")
    atn.add_argument("--out", default="out/atn")
    atn.add_argument("--audit", default=None)
    atnsub = atn.add_subparsers(dest="atn_action", required=True)
    atnd = atnsub.add_parser("draft", help="собрать черновик АТН из позиций с основаниями")
    atnd.add_argument("--items", required=True,
                      help="JSON позиций: [{code, name, unit, qty, price, basis{kind, ref}}]")
    atnd.add_argument("--acts", default=None, help="реестр актов: что уже предъявлено")
    atnd.add_argument("--estimate", default=None, help="смета: цена и остаток по коду")
    atnd.add_argument("--number", default="", help="номер акта")
    atnd.add_argument("--object-name", default="")
    atnd.add_argument("--contract", default="")
    atnd.add_argument("--customer", default="")
    atnd.add_argument("--contractor", default="")
    atnd.add_argument("--reason", default="", help="основание выпуска акта")
    atnd.set_defaults(func=cmd_atn)
    atnc = atnsub.add_parser("confirm", help="отметить проверку человеком (ФИО и роль)")
    atnc.add_argument("--atn", required=True, help="JSON черновика АТН")
    atnc.add_argument("--by", required=True)
    atnc.add_argument("--role", required=True)
    atnc.add_argument("--reason", default="")
    atnc.set_defaults(func=cmd_atn)

    tkc = sub.add_parser("tk", help="черновик технологической карты или ППР (без выдуманных норм)")
    tkc.add_argument("--out", default="out/tk")
    tkc.add_argument("--audit", default=None, help="журнал аудита (по умолчанию не пишем)")
    tkcsub = tkc.add_subparsers(dest="tk_action", required=True)
    for name, help_text in (("draft", "черновик ТК по виду работ"),
                            ("ppr", "черновик ППР по объекту")):
        sub_p = tkcsub.add_parser(name, help=help_text)
        sub_p.add_argument("--object-name", default="")
        sub_p.add_argument("--estimate", default="", help="смета (xlsx): что учтено сметой")
        sub_p.add_argument("--norms", default=None, help="каталог нормативной базы")
        if name == "draft":
            sub_p.add_argument("--work-type", default="", help="id вида работ из матрицы")
            sub_p.add_argument("--text", default="", help="текст работы (классификатор)")
        else:
            sub_p.add_argument("--work-type", default="", help=argparse.SUPPRESS)
            sub_p.add_argument("--text", default="", help=argparse.SUPPRESS)
        sub_p.set_defaults(func=cmd_tk)
    tkc_reg = tkcsub.add_parser("registry",
                                help="реестр ТК объекта: объект → ТК → статус → кто проверил")
    tkc_reg.add_argument("--paths", nargs="*", default=[],
                         help="каталоги или JSON черновиков (по умолчанию --out)")
    tkc_reg.add_argument("--strict", action="store_true",
                         help="код 2, если есть карты без проверки человеком")
    tkc_reg.set_defaults(func=cmd_tk)
    tkc_confirm = tkcsub.add_parser("confirm", help="отметить проверку человеком")
    tkc_confirm.add_argument("--draft", required=True, help="JSON черновика")
    tkc_confirm.add_argument("--by", required=True, help="ФИО проверившего")
    tkc_confirm.add_argument("--role", required=True, help="должность/роль")
    tkc_confirm.add_argument("--filled", default=None,
                             help="JSON {раздел: [что вписал инженер]}")
    tkc_confirm.add_argument("--reason", default="")
    tkc_confirm.set_defaults(func=cmd_tk)

    tk = sub.add_parser("takeoff", help="объёмы из чертежей (BOQ) → ведомость с ценами из базы")
    tk.add_argument("--out", default="out/takeoff")
    tksub = tk.add_subparsers(dest="takeoff_action", required=True)
    tkd = tksub.add_parser("draft", help="собрать черновик ведомости из выгрузки объёмов")
    tkd.add_argument("--boq", required=True, help="выгрузка объёмов (csv, xlsx)")
    tkd.add_argument("--map", default=None, help="словарь «элемент → код ресурса»")
    tkd.add_argument("--prices-dir", default="data/prices")
    tkd.add_argument("--as-of", default=None, help="цены на дату (не свежее этой даты)")
    tkd.add_argument("--object-name", default="")
    tkd.add_argument("--section", default="КР")
    tkd.set_defaults(func=cmd_takeoff)
    tkp = tksub.add_parser("pdf", help="раздел КР из PDF: факты, объёмы расчётом, черновик")
    tkp.add_argument("--file", required=True, help="PDF раздела КР")
    tkp.add_argument("--map", default=None)
    tkp.add_argument("--prices-dir", default="data/prices")
    tkp.add_argument("--as-of", default=None)
    tkp.add_argument("--object-name", default="")
    tkp.add_argument("--section", default="КР1")
    tkp.add_argument("--profile", default=None,
                     help="профиль разбора PDF: kr, sks, ovik (по умолчанию — из файла профилей)")
    tkp.add_argument("--profiles", default=None, help="путь к pdf_profiles.json")
    tkp.add_argument("--cache", default=None, help="кэш текста страниц (json)")
    tkp.set_defaults(func=cmd_takeoff)
    stk = sub.add_parser("stock", help="складской контур: движения, партии, остатки")
    stks = stk.add_subparsers(dest="stock_action", required=True)

    stk_demo = stks.add_parser("demo", help="собрать демо-склад (два склада, приходы, списания)")
    stk_demo.add_argument("--state", default="out/stock/state.json")

    stk_demo.set_defaults(func=cmd_stock)

    stk_post = stks.add_parser("post", help="провести документы (приход, М-11, М-8, М-15, списание)")
    stk_post.add_argument("--state", default="out/stock/state.json")
    stk_post.add_argument("--docs", required=True, help="JSON: документ или список документов")
    stk_post.add_argument("--allow-negative", dest="allow_negative", action="store_true",
                          help="провести с отрицательным остатком (нужна --reason)")
    stk_post.add_argument("--reason", default="", help="причина разрешения отрицательного остатка")
    stk_post.add_argument("--closed-before", dest="closed_before", default=None,
                          help="период закрыт по дату: проведение в него запрещено")
    stk_post.set_defaults(func=cmd_stock)

    stk_inv = stks.add_parser("inventory", help="инвентаризация ИНВ-3 из CSV: факт против учёта")
    stk_inv.add_argument("--state", default="out/stock/state.json")
    stk_inv.add_argument("--file", required=True)
    stk_inv.add_argument("--warehouse", required=True)
    stk_inv.add_argument("--date", required=True)
    stk_inv.add_argument("--number", default="")
    stk_inv.add_argument("--allow-negative", dest="allow_negative", action="store_true")
    stk_inv.add_argument("--reason", default="")
    stk_inv.set_defaults(func=cmd_stock)

    stk_bal = stks.add_parser("balances", help="остатки по складам")
    stk_bal.add_argument("--state", default="out/stock/state.json")
    stk_bal.add_argument("--warehouse", default=None)
    stk_bal.add_argument("--as-of", dest="as_of", default=None)
    stk_bal.add_argument("--daval", action="store_true", default=False,
                         help="показать только давальческие материалы (счёт 003)")
    stk_bal.add_argument("--all", dest="all_contours", action="store_true",
                         help="показать оба контура: собственный и давальческий")
    stk_bal.set_defaults(func=cmd_stock)

    stk_moves = stks.add_parser("moves", help="журнал движений")
    stk_moves.add_argument("--state", default="out/stock/state.json")
    stk_moves.add_argument("--item", default=None)
    stk_moves.add_argument("--warehouse", default=None)
    stk_moves.add_argument("--limit", type=int, default=50)
    stk_moves.set_defaults(func=cmd_stock)

    stk_ver = stks.add_parser("verify", help="сходится ли учёт: остатки против движений")
    stk_ver.add_argument("--state", default="out/stock/state.json")
    stk_ver.set_defaults(func=cmd_stock)

    stk_pr = stks.add_parser("print",
                             help="печатные бланки документов: М-4, М-11, М-8, М-15, ИНВ-3, "
                                  "акт списания, акт давальческих")
    stk_pr.add_argument("--state", default="out/stock/state.json")
    stk_pr.add_argument("--docs", required=True, help="JSON: документ или список документов")
    stk_pr.add_argument("--form", default=None,
                        help="бланк принудительно (m4, m11, m8, m15, inv3, writeoff, daval)")
    stk_pr.add_argument("--out", default="out/forms/stock")
    stk_pr.set_defaults(func=cmd_stock)

    stk_m29 = stks.add_parser("m29", help="материальный отчёт М-29 из движений склада")
    stk_m29.add_argument("--state", default="out/stock/state.json")
    stk_m29.add_argument("--period", required=True)
    stk_m29.add_argument("--out", default="out/forms")
    stk_m29.set_defaults(func=cmd_stock)

    fm = sub.add_parser("forms", help="печатные формы КС-3, КС-6а, М-29 из артефактов прогона")
    fms = fm.add_subparsers(dest="form", required=True)
    common = [("--period", {"required": True, "help": "отчётный период: 2026-09 или ISO-дата"}),
              ("--out", {"default": "out/forms"}),
              ("--object-name", {"default": ""}), ("--contract", {"default": ""}),
              ("--customer", {"default": ""}), ("--contractor", {"default": ""})]
    fk2 = fms.add_parser("ks2", help="акт о приёмке выполненных работ (КС-2) по итогам run")
    fk2.add_argument("--summary", default="out/summary.json",
                     help="итог прогона run (позиции, объёмы, ограничения)")
    fk2.add_argument("--vat-rate", default=None, help="ставка НДС из договора (20, 10, 0)")
    fk2.add_argument("--number", default="", help="номер акта")
    for flag, options in common:
        fk2.add_argument(flag, **options)
    fk2.set_defaults(func=cmd_forms)
    fk3 = fms.add_parser("ks3", help="справка о стоимости выполненных работ (КС-3)")
    fk3.add_argument("--acts", default="data/demo/acts/acts.json")
    fk3.add_argument("--vat-rate", default=None, help="ставка НДС из договора (20, 10, 0)")
    for flag, options in common:
        fk3.add_argument(flag, **options)
    fk3.set_defaults(func=cmd_forms)
    fk6 = fms.add_parser("ks6a", help="журнал учёта выполненных работ (КС-6а)")
    fk6.add_argument("--acts", default="data/demo/acts/acts.json")
    fk6.add_argument("--works", default=None, help="справочник «код → наименование»")
    for flag, options in common:
        fk6.add_argument(flag, **options)
    fk6.set_defaults(func=cmd_forms)
    fm2 = fms.add_parser("m29", help="отчёт о расходе материалов (М-29)")
    fm2.add_argument("--purchases", default="data/demo/consumption/purchases.csv")
    fm2.add_argument("--writeoffs", default="data/demo/consumption/writeoffs.csv")
    fm2.add_argument("--norms", default="data/demo/consumption/norms.json")
    fm2.add_argument("--works", default="data/demo/consumption/works.json")
    fm2.add_argument("--opening", default=None, help='остаток на начало: json {материал: количество}')
    fm2.add_argument("--stock", default=None,
                     help="состояние склада: приход/расход/остаток берутся из движений, "
                          "а не из CSV (тогда --purchases/--writeoffs не нужны)")
    for flag, options in common:
        fm2.add_argument(flag, **options)
    fm2.set_defaults(func=cmd_forms)

    tks = tksub.add_parser("schedules",
                           help="ведомости расхода стали и спецификации → проверка и черновик")
    tks.add_argument("--schedules", default="data/input/kr1_schedules.json",
                     help="ведомости в JSON (перенесённые из листов проекта)")
    tks.add_argument("--pdf-report", default=None,
                     help="отчёт прогона PDF того же тома — для сверки «габариты vs ведомость»")
    tks.add_argument("--map", default="data/reference/takeoff_map_kr1.json")
    tks.add_argument("--prices-dir", default="data/demo/prices_kr1")
    tks.add_argument("--as-of", default=None)
    tks.add_argument("--object-name", default="")
    tks.add_argument("--section", default="КР1")
    tks.set_defaults(func=cmd_takeoff)
    tkt = tksub.add_parser("tz", help="таблицы технического задания (СКС, слаботочка) → объёмы")
    tkt.add_argument("--file", required=True, help="PDF технического задания")
    tkt.add_argument("--map", default="data/reference/takeoff_map_tz.json")
    tkt.add_argument("--prices-dir", default="data/prices")
    tkt.add_argument("--as-of", default=None)
    tkt.add_argument("--object-name", default="")
    tkt.add_argument("--section", default="ТЗ")
    tkt.add_argument("--cache", default=None)
    tkt.add_argument("--no-draft", dest="tz_no_draft", action="store_true",
                     help="только объёмы из ТЗ, без стыка с ценами")
    tkt.set_defaults(func=cmd_takeoff)
    tkc = tksub.add_parser("confirm", help="подтвердить ведомость: количества и шифры расценок")
    tkc.add_argument("--draft", required=True)
    tkc.add_argument("--changes", default=None, help="JSON {элемент: {qty, norm, exclude, note}}")
    tkc.add_argument("--set", nargs="*", default=[], help="элемент=количество (можно несколько)")
    tkc.add_argument("--by", required=True)
    tkc.add_argument("--role", default="инженер ПТО")
    tkc.add_argument("--reason", default="")
    tkc.add_argument("--audit", default=None)
    tkc.set_defaults(func=cmd_takeoff)

    au = sub.add_parser("audit", help="журнал аудита: лента изменений, запись, выгрузка")
    au.add_argument("--path", default="out/audit.jsonl")
    ausub = au.add_subparsers(dest="audit_action", required=True)
    aua = ausub.add_parser("anchor", help="зафиксировать якорь хвоста журнала")
    aua.add_argument("--path", default="out/audit.jsonl")
    aua.set_defaults(func=cmd_audit)
    aul = ausub.add_parser("list", help="лента записей")
    aul.add_argument("--entity", default=None)
    aul.add_argument("--limit", type=int, default=None)
    aul.set_defaults(func=cmd_audit)
    aus = ausub.add_parser("show", help="показать запись по началу хэша")
    aus.add_argument("hash")
    aus.set_defaults(func=cmd_audit)
    aue = ausub.add_parser("export", help="выгрузить ленту в xlsx")
    aue.add_argument("--output", default="out/audit_report.xlsx")
    aue.add_argument("--entity", default=None)
    aue.set_defaults(func=cmd_audit)

    a = sub.add_parser("audit-verify", help="проверить целостность журнала аудита")
    a.add_argument("--path", default="out/audit.jsonl")
    a.add_argument("--anchor", action="store_true",
                   help="сверить хвост с якорем (ловит удаление последних записей)")
    a.set_defaults(func=cmd_audit_verify)

    ap = sub.add_parser("approve", help="зафиксировать решение человека по черновику акта (HITL)")
    ap.add_argument("--out", default="out")
    ap.add_argument("--by", required=True, help="ФИО инженера ПТО")
    ap.add_argument("--role", default="инженер ПТО")
    ap.add_argument("--decision", default="approved",
                    choices=["approved", "rejected", "returned_for_revision"])
    ap.add_argument("--comment", default="", help="обоснование, обязательно при открытых критичных замечаниях")
    ap.set_defaults(func=cmd_approve)

    cad = sub.add_parser("cad", help="CAD-модуль: DXF, шаблоны, операции, сверка объёмов")
    cad.add_argument("cad_args", nargs=argparse.REMAINDER)

    mcp = sub.add_parser("mcp", help="MCP-сервер CAD-инструментов (stdio или http)")
    mcp.add_argument("mcp_args", nargs=argparse.REMAINDER)

    rt = sub.add_parser("route", help="предпросмотр маршрутизации модели")
    rt.add_argument("--intent", default="ks2_validate")
    rt.add_argument("--data-class", default="internal",
                    choices=["public", "internal", "commercial_secret", "personal"])
    rt.add_argument("--images", action="store_true")
    rt.add_argument("--complexity", default="high", choices=["low", "medium", "high"])
    rt.set_defaults(func=cmd_route)

    ex1 = sub.add_parser("export-1c", help="проверенная КС-2 → CSV для 1С/БИТ.Строительство")
    ex1.add_argument("--summary", default="out/summary.json")
    ex1.add_argument("--out", default="out/1c/ks2_for_1c.csv")
    ex1.add_argument("--quantity", default="claim",
                     choices=["claim", "journal", "estimate"],
                     help="какой объём выгружать: к предъявлению / по журналу / по смете")
    ex1.set_defaults(func=cmd_export_1c)

    im1 = sub.add_parser("import-1c", help="CSV из 1С/Гранд-Сметы → смета и реестр актов")
    im1.add_argument("--smeta", help="CSV со строками сметы")
    im1.add_argument("--acts", help="CSV с реестром актов КС-2")
    im1.add_argument("--object-name", default="из 1С")
    im1.add_argument("--out", default="out/1c")
    im1.set_defaults(func=cmd_import_1c)

    sy = sub.add_parser("sync-1c", help="обмен с 1С по HTTP: акты из 1С, проверенная КС-2 обратно")
    sy.add_argument("--config", default="config/one_c.example.yaml",
                    help="конфиг HTTP-сервиса 1С (значения ${VAR} берутся из окружения)")
    sy.add_argument("--period", help="период актов, YYYY-MM")
    sy.add_argument("--act-id", help="конкретный акт")
    sy.add_argument("--summary", default="out/summary.json")
    sy.add_argument("--push", action="store_true", help="отправить проверенную КС-2 в 1С")
    sy.add_argument("--quantity", default="claim", choices=["claim", "journal", "estimate"])
    sy.add_argument("--object-name", default=None)
    sy.add_argument("--out", default="out/1c")
    sy.set_defaults(func=cmd_sync_1c)

    rv = sub.add_parser("review", help="сводка проверки: что проверено, что осталось, где решение человека")
    rv.add_argument("--summary", default="out/summary.json")
    rv.add_argument("--batch", default="out/cad/batch/batch_summary.json")
    rv.add_argument("--acts", default="data/demo/acts/acts.json")
    rv.add_argument("--impact", default="out/cad/impact/impact.json")
    rv.add_argument("--plan", default="out/cad/plan.json")
    rv.add_argument("--materials", default="out/materials/materials.json",
                    help="сверка материалов «заявка → приход → КС-2»")
    rv.add_argument("--object-name", default=None)
    rv.add_argument("--out", default="out/review")
    rv.set_defaults(func=cmd_review)

    args = p.parse_args(argv)
    result = args.func(args)
    if args.cmd in ("audit-verify", "approve", "prices", "materials", "procure", "audit",
                    "takeoff", "tk", "cumulative", "atn", "consumption", "checklist",
                    "journal", "quality", "docflow", "correspondence", "pack", "finance",
                    "monthly", "forms", "stock"):
        return result
    return 0
