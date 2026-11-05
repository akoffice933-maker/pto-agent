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
from .parsers import read_closed, read_estimate, read_journal
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
    items = read_estimate(data / "smeta_v1.xlsx")
    entries = read_journal(data / "journal_2026_10.xlsx")
    closed = read_closed(data / "acts_previous.xlsx")
    audit.append("agent:parser", "agent", "documents.read", "dataset",
                 {"estimate_items": len(items), "journal_entries": len(entries),
                  "closed_records": len(closed)})
    print(f"   смета: {len(items)} позиций · журнал: {len(entries)} записей · "
          f"закрыто ранее: {len(closed)} записей")

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
        })
    return out


def cmd_norms(args):
    index = NormIndex(load_norms(Path(args.data) / "norms"))
    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()
    answer = index.ask(args.query, as_of=as_of)
    print(json.dumps({"query": args.query, **answer.as_dict()}, ensure_ascii=False, indent=2))
    return answer


def cmd_audit_verify(args):
    ok, count, msg = AuditLog(Path(args.path)).verify()
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


# ─────────────────────────── журнал аудита ───────────────────────────
def cmd_audit(args) -> int:
    log = AuditLog(Path(args.path))
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

    au = sub.add_parser("audit", help="журнал аудита: лента изменений, запись, выгрузка")
    au.add_argument("--path", default="out/audit.jsonl")
    ausub = au.add_subparsers(dest="audit_action", required=True)
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
    if args.cmd in ("audit-verify", "approve", "prices", "materials", "procure", "audit"):
        return result
    return 0
