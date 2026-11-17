#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собрать демо-журнал за 2026 год для закрытия года (круг 41).

Что делает: берёт явные демо-суммы из `data/demo/year_close/scenario_2026.json`, проводит
12 месяцев выручки (КС-2, подписанные заказчиком) и субподряда, недостачу, оценочное
обязательство и РБП, закрывает каждый месяц тем же `closing.close_month`, что и в контуре,
и сохраняет журнал в `out/year_close/ledger_2026.json`.

Ни одна цифра результата здесь не пишется руками: прибыль, НДС и остатки считают
`ledger` и `closing`. Демо — это только ввод (суммы актов), и он помечен в файле данных.

    python3 scripts/make_demo_year.py [--out out/year_close/ledger_2026.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pto_agent import closing, ledger                                # noqa: E402

SCENARIO = ROOT / "data" / "demo" / "year_close" / "scenario_2026.json"


def _post(state: dict, *, event: str, date: str, number: str, lines: list[dict], memo: str,
          key: str, kind: str = "demo") -> dict:
    result = ledger.post_entry(
        state, event=event, date=date,
        doc={"number": number, "date": date, "kind": kind}, lines=lines, memo=memo,
        rule=event, key=key)
    if result["status"] != ledger.STATUS_OK:
        raise SystemExit(f"{number}: {result['errors'] or result['warnings']}")
    return result


def build(scenario_path: Path = SCENARIO) -> tuple[dict, dict]:
    payload = json.loads(scenario_path.read_text(encoding="utf-8"))
    state = ledger.new_state(object_name=payload["object"], organization="ООО «ПТО-Строй»",
                             contract=payload["contract"])
    acts = []
    for row in payload["months"]:
        period, net = row["period"], float(row["net"])
        rate = payload["vat"]["rate"]
        last_day = closing._period_end(period)
        act_number = f"КС-2 №{int(period[-2:]) + 40}"
        # сумма акта — без НДС (vat.included = false): НДС контур начисляет сверху сам
        acts.append({"act_id": act_number, "act_date": last_day, "status": "подписан",
                     "amount_rub": net, "customer": payload["customer"],
                     "contract": payload["contract"],
                     "vat": {"rate": rate, "included": False}})
        _post(state, event="demo.cost", date=last_day, number=f"акт субподряда {period}",
              lines=[{"account": "20.01", "side": ledger.DEBIT, "amount": row["cost"],
                      "analytics": {"объект": payload["object"]}},
                     {"account": "60.01", "side": ledger.CREDIT, "amount": row["cost"],
                      "analytics": {"контрагент": payload["subcontractor"]}}],
              memo=f"субподряд за {period} без НДС (исполнитель на УСН): "
                   f"{row['cost']:,.2f} ₽",
              key=f"demo.cost|{period}")

    posted = ledger.post_acts(state, {"acts": acts}, contract=payload["contract"],
                              customer=payload["customer"])
    if posted["errors"]:
        raise SystemExit(f"акты: {posted['errors']}")

    shortage = payload["shortage"]
    _post(state, event="demo.shortage", date=shortage["date"],
          number=shortage["document"]["number"],
          lines=[{"account": "94", "side": ledger.DEBIT, "amount": shortage["amount"],
                  "analytics": {"склад": shortage["warehouse"]}},
                 {"account": "10.01", "side": ledger.CREDIT, "amount": shortage["amount"],
                  "analytics": {"склад": shortage["warehouse"],
                                "номенклатура": shortage["item"]}}],
          memo=f"недостача по ИНВ-3: {shortage['note']}",
          key=f"demo.shortage|{shortage['date']}")

    provision = payload["provision"]
    _post(state, event="demo.provision", date=provision["date"],
          number=provision["document"]["number"],
          lines=[{"account": "26", "side": ledger.DEBIT, "amount": provision["amount"],
                  "analytics": {"статья затрат": provision["article"]}},
                 {"account": "96", "side": ledger.CREDIT, "amount": provision["amount"],
                  "analytics": {"вид обязательства": provision["kind"]}}],
          memo=f"оценочное обязательство: {provision['note']}",
          key=f"demo.provision|{provision['date']}")

    future = payload["future_cost"]
    _post(state, event="demo.future_cost", date=future["date"],
          number=future["document"]["number"],
          lines=[{"account": "97", "side": ledger.DEBIT, "amount": future["amount"],
                  "analytics": {"вид расхода": future["kind"]}},
                 {"account": "60.01", "side": ledger.CREDIT, "amount": future["amount"],
                  "analytics": {"контрагент": future["counterparty"]}}],
          memo=f"расходы будущих периодов: {future['note']}",
          key=f"demo.future_cost|{future['date']}")

    months = []
    for row in payload["months"]:
        report = closing.close_month(state, row["period"])
        months.append({"period": row["period"], "closed": report.get("closed"),
                       "problems": [item["code"] for item in report.get("problems") or []],
                       "profit": (report.get("result") or {}).get("profit")})
    return state, {"object": payload["object"], "months": months,
                   "verify": ledger.verify(state)["ok"],
                   "entries": len(state.get("entries") or [])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="демо-журнал за 2026 год для закрытия года")
    parser.add_argument("--out", default="out/year_close/ledger_2026.json")
    parser.add_argument("--scenario", default=str(SCENARIO))
    args = parser.parse_args(argv)
    state, summary = build(Path(args.scenario))
    path = (ROOT / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    ledger.save(state, path)
    print(json.dumps({**summary, "saved": str(path)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
