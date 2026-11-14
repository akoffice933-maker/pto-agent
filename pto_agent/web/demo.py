# -*- coding: utf-8 -*-
"""Демонстрационный контур для экранов: склад + казначейство + журнал проводок.

Ничего не выдумывается: склад берётся из `data/demo/stock`, казначейство — из
`data/demo/treasury`, проводки строятся теми же функциями, что и в CLI. Экран нужно
на чём-то показать — значит, состояние должно быть настоящим, а не нарисованным.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import ledger, payroll, stock, treasury
from . import auth

REPO = Path(__file__).resolve().parents[2]


def _load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build(root: str | Path, *, demo_dir: str | Path | None = None,
          data_dir: str | Path | None = None) -> dict:
    """Собрать состояния контуров в `root` и вернуть краткий отчёт о том, что вышло."""
    root = Path(root)
    demo_dir = Path(demo_dir or REPO / "data" / "demo")
    data_dir = Path(data_dir or REPO / "data" / "reference")

    catalog = _load(demo_dir / "stock" / "catalog.json")
    documents = _load(demo_dir / "stock" / "documents_2026_10.json")

    stock_state = stock.new_state(object_name=catalog["object"],
                                  warehouses=catalog["warehouses"],
                                  items=catalog["items"],
                                  opening=catalog.get("opening"))
    posted = stock.post_many(stock_state, [dict(doc) for doc in documents["documents"]])
    stock.save(stock_state, root / "stock" / "state.json")

    ledger_state = ledger.new_state(object_name=catalog["object"],
                                    organization=catalog.get("organization") or "",
                                    contract=catalog.get("contract") or "")
    ledger.post_stock(ledger_state, stock_state, documents=documents)

    cash_payload = _load(demo_dir / "treasury" / "state_2026_10.json")
    cash_state = treasury.new_state(object_name=cash_payload.get("object") or "",
                                    opening=cash_payload.get("opening"),
                                    limits=cash_payload.get("limits") or {},
                                    contract=cash_payload.get("contract") or "")
    cash_state["object_limits"] = cash_payload.get("object_limits") or {}
    for row in cash_payload.get("requests") or []:
        treasury.add_request(cash_state, dict(row))
    treasury.save(cash_state, root / "treasury" / "state.json")

    # ── зарплата: табель → начисление → проводки (те же функции, что в CLI) ──
    staff_payload = _load(demo_dir / "payroll" / "staff.json")
    payroll_rules = payroll.load_rules()
    payroll_state = payroll.new_state(object_name=staff_payload.get("object") or "")
    payroll_done = []
    for period in ("2026-09", "2026-10"):
        path = demo_dir / "payroll" / f"timesheet_{period.replace('-', '_')}.json"
        if not path.exists():
            continue
        report = payroll.accrue(payroll_state, _load(path), staff_payload, payroll_rules)
        if report.get("errors"):
            continue
        payroll.remember(payroll_state, report, rules=payroll_rules)
        payroll_done.append({"period": period, "accrued": report["totals"]["accrued"],
                             "net": report["totals"]["net"]})
    last = next((row for row in payroll_state["periods"]
                 if row.get("period") == "2026-10"), None)
    if last:
        payroll.post_ledger(ledger_state, last)
    payroll.save_state(payroll_state, root / "payroll" / "state.json")

    ledger.save(ledger_state, root / "ledger" / "state.json")

    users_path = Path(data_dir) / "users.json"
    if not users_path.exists():
        users_path.write_text(json.dumps(auth.build_demo_users(), ensure_ascii=False,
                                         indent=2) + "\n", encoding="utf-8")

    return {
        "root": str(root),
        "stock": {"documents": posted.get("posted"), "moves": len(stock_state["moves"]),
                  "rows": len(stock.balances(stock_state))},
        "treasury": {"requests": len(cash_state.get("requests") or []),
                     "paid": len(cash_state.get("payments") or [])},
        "payroll": {"periods": payroll_done,
                    "net": round(sum(float(row["net"]) for row in payroll_done), 2)},
        "ledger": {"entries": len(ledger_state.get("entries") or []),
                   "balanced": ledger.verify(ledger_state)["ok"]},
        "users": len(auth.load(users_path).get("users") or []),
    }
