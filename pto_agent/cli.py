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

    args = p.parse_args(argv)
    result = args.func(args)
    if args.cmd in ("audit-verify", "approve"):
        return result
    return 0
