# -*- coding: utf-8 -*-
"""CLI MVP ИИ-агента ПТО."""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from .audit import AuditLog
from .export import (export_discrepancies, export_discrepancies_csv, export_ks2,
                     export_remainder)
from .llm import describe_discrepancies, route
from .norms import NormIndex, load_norms
from .parsers import read_closed, read_estimate, read_journal
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
                     impact_path=args.impact, plan_path=args.plan, object_name=args.object_name)
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


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # подкоманды CAD и MCP живут в своих пакетах: делегируем до разбора argparse,
    # чтобы их собственные ключи (--transport, --mode и т.п.) не перехватывались
    if argv and argv[0] == "cad":
        from .cad.cli import main as cad_main
        return cad_main(argv[1:])
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
    rv.add_argument("--object-name", default=None)
    rv.add_argument("--out", default="out/review")
    rv.set_defaults(func=cmd_review)

    args = p.parse_args(argv)
    result = args.func(args)
    if args.cmd in ("audit-verify", "approve"):
        return result
    return 0
