# -*- coding: utf-8 -*-
"""Сверка результата прогона с эталоном data/demo/expected.json."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
expected = json.loads((root / "data/demo/expected.json").read_text(encoding="utf-8"))
summary_path = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "out/summary.json"
if not summary_path.exists():
    sys.exit("Сначала выполните: python3 -m pto_agent run --data data/demo --out out")
summary = json.loads(summary_path.read_text(encoding="utf-8"))

found = {(d["type"], d["code"]) for d in summary["discrepancies"]}
exp = expected["expected_discrepancies"]
hit = [e for e in exp if (e["type"], e["code"]) in found]
miss = [e for e in exp if (e["type"], e["code"]) not in found]
extra = [(d["type"], d["code"]) for d in summary["discrepancies"]
         if (d["type"], d["code"]) not in {(e["type"], e["code"]) for e in exp}]

print(f"Эталон: {len(exp)} расхождений · найдено: {len(hit)} · пропущено: {len(miss)} · лишних: {len(extra)}")
for e in hit:
    print(f"  [ok]   {e['type']:<22} {e['code'] or '—':<20} {e['note'][:60]}")
for e in miss:
    print(f"  [MISS] {e['type']:<22} {e['code'] or '—':<20} {e['note'][:60]}")
for t, c in extra:
    print(f"  [EXTRA] {t:<21} {c or '—'}")

tot = expected.get("expected_totals", {})
ok_tot = True
if "amount_estimate" in tot:
    ok = abs(summary["amount_estimate"] - tot["amount_estimate"]) < 0.5
    ok_tot &= ok
    print(f"Сметная стоимость: {summary['amount_estimate']:,.2f} (эталон {tot['amount_estimate']:,.2f}) — {'ok' if ok else 'РАСХОЖДЕНИЕ'}")
if "positions_in_act" in tot:
    ok = summary["positions_in_act"] == tot["positions_in_act"]
    ok_tot &= ok
    print(f"Позиций в акте: {summary['positions_in_act']} (эталон {tot['positions_in_act']}) — {'ok' if ok else 'РАСХОЖДЕНИЕ'}")

sys.exit(0 if not miss and not extra and ok_tot else 1)
