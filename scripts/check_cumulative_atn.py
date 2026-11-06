#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сквозная проверка накопительной ведомости и АТН на настоящем CLI: 6 правил, код 2 при нарушении.

    python scripts/check_cumulative_atn.py [--keep] [--json]

Правила:

1. сумма акта и сумма позиций показываются обе, расхождение — ошибка с кодом возврата 2;
2. акт без позиций виден как «без построчного подтверждения», а не растворяется в итоге;
3. дубль акта не удваивает сумму;
4. в АТН попадают только позиции с основанием и ценой; без основания — в отдельный список
   со своей суммой;
5. нет цены — сумма не считается (не ноль рублей);
6. подтверждение АТН без ФИО и роли не принимается.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def build_fixture(work: Path) -> Path:
    acts = {
        "object": "Проверочный объект", "contract": "Д-1",
        "acts": [
            {"act_id": "КС-2 №1", "period": "сентябрь 2026", "amount_rub": 1850000.0,
             "positions": [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 100,
                            "price": 18500}]},
            {"act_id": "КС-2 №1", "period": "сентябрь 2026", "amount_rub": 1850000.0,
             "positions": [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 100,
                            "price": 18500}]},
            {"act_id": "КС-2 №2", "period": "октябрь 2026", "amount_rub": 370000.0,
             "positions": [{"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 20,
                            "price": 18500}]},
            {"act_id": "КС-2 №3", "period": "октябрь 2026", "amount_rub": 34106600.0,
             "positions": []},
        ],
    }
    path = work / "acts.json"
    path.write_text(json.dumps(acts, ensure_ascii=False), encoding="utf-8")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="сквозная проверка ведомости и АТН")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="cumulative-atn-"))
    problems: list[str] = []

    def run(*argv: str, expect: int | None = 0) -> subprocess.CompletedProcess:
        result = cli(*argv)
        if expect is not None and result.returncode != expect:
            problems.append(f"{' '.join(argv[:3])}: код {result.returncode}, ожидался {expect}\n"
                            f"{result.stdout[-500:]}{result.stderr[-400:]}")
        return result

    acts = build_fixture(work)
    out = work / "cumulative"
    # в наборе намеренно есть дубль акта и акт без позиций — код 2 здесь ожидаемый
    run("cumulative", "--acts", str(acts), "--out", str(out), expect=2)
    ledger = json.loads((out / "cumulative.json").read_text(encoding="utf-8"))
    codes = [c["code"] for c in ledger["checks"]]

    # правило 1
    if ledger["totals"]["difference_rub"] <= 0:
        problems.append("правило 1: расхождение «сумма актов − сумма позиций» не показано")
    bad = work / "acts_bad.json"
    broken = json.loads(acts.read_text(encoding="utf-8"))
    broken["acts"][0]["amount_rub"] = 1.0
    bad.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    failed = run("cumulative", "--acts", str(bad), "--out", str(work / "bad"), expect=2)
    if "ACT_SUM_MISMATCH" not in failed.stdout:
        problems.append("правило 1: расхождение суммы акта и позиций не названо ошибкой")

    # правило 2
    if "NO_POSITIONS" not in codes:
        problems.append("правило 2: акт без позиций не помечен")
    # правило 3
    if "DUPLICATE_ACT" not in codes:
        problems.append("правило 3: дубль акта не обнаружен")
    if ledger["totals"]["acts"] != 3:
        problems.append(f"правило 3: учтено актов {ledger['totals']['acts']}, ожидалось 3")

    # правила 4–5
    items = work / "items.json"
    items.write_text(json.dumps([
        {"code": "ФЕР 06-01-001-01", "unit": "м3", "qty": 10, "price": 18500,
         "basis": {"kind": "лист РД", "ref": "КР1, лист 12"}},
        {"code": "БЕЗ-ОСНОВАНИЯ", "unit": "шт", "qty": 3, "price": 100},
        {"code": "БЕЗ-ЦЕНЫ", "unit": "шт", "qty": 2,
         "basis": {"kind": "письмо", "ref": "исх. № 5"}},
    ], ensure_ascii=False), encoding="utf-8")
    atn_out = work / "atn"
    run("atn", "--out", str(atn_out), "draft", "--items", str(items), "--number", "АТН-1",
        expect=2)
    atn = json.loads((atn_out / "atn_АТН-1.json").read_text(encoding="utf-8"))
    if atn["totals"]["to_claim_rub"] != 185000:
        problems.append(f"правило 4: к предъявлению {atn['totals']['to_claim_rub']}, "
                        f"ожидалось 185000 (только позиция с основанием и ценой)")
    if atn["totals"]["needs_basis"] != 1 or not atn["totals"]["needs_basis_rub"]:
        problems.append("правило 4: позиция без основания не попала в отдельный список "
                        "со своей суммой")
    if atn["totals"]["no_price"] != 1:
        problems.append("правило 5: позиция без цены не вынесена в «нет цены»")
    if any(p.get("amount_rub") == 0 for p in atn["no_price"]):
        problems.append("правило 5: у позиции без цены появилась сумма 0 ₽")

    # правило 6
    anonymous = run("atn", "--out", str(atn_out), "confirm", "--atn",
                    str(atn_out / "atn_АТН-1.json"), "--by", " ", "--role", " ", expect=2)
    if "ОСТАНОВЛЕНО" not in anonymous.stdout:
        problems.append("правило 6: анонимное подтверждение не остановлено")

    summary = {
        "правил": 6,
        "нарушений": len(problems),
        "актов": ledger["totals"]["acts"],
        "сумма_актов": ledger["totals"]["amount_acts_rub"],
        "сумма_позиций": ledger["totals"]["amount_positions_rub"],
        "позиций_в_атн": atn["totals"]["included"],
        "к_предъявлению_руб": atn["totals"]["to_claim_rub"],
        "требует_основания_руб": atn["totals"]["needs_basis_rub"],
    }
    if args.json:
        print(json.dumps({**summary, "problems": problems}, ensure_ascii=False, indent=1))
    else:
        print(f"правил: 6 · актов: {summary['актов']} · сумма актов: "
              f"{summary['сумма_актов']:,.2f} ₽ · сумма позиций: "
              f"{summary['сумма_позиций']:,.2f} ₽")
        print(f"АТН: позиций с основанием {summary['позиций_в_атн']} · к предъявлению "
              f"{summary['к_предъявлению_руб']:,.2f} ₽ · требует основания "
              f"{summary['требует_основания_руб']:,.2f} ₽")
        print(f"нарушений: {len(problems)}")
        for problem in problems:
            print(f"  ✗ {problem}")

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
