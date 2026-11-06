#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сквозная проверка норм расхода, списания, лимитов и давальческих на настоящем CLI.

    python scripts/check_consumption.py [--keep] [--json]

Правила:

1. норма без документа, пункта и редакции не применяется (потребность по ней не считается);
2. без подтверждения норм человеком потребность идёт как «справочная», подтверждённая = 0;
   с ФИО и ролью — переходит в подтверждённую, и это видно в отчёте;
3. перерасход сверх нормы без причины — ошибка с суммой; с причиной — предупреждение;
4. списание без нормы не сравнивается с нулём — отдельная ошибка;
5. закупка сверх лимита — ошибка с числами;
6. давальческий материал в собственных закупках — двойной счёт (ошибка).
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
DEMO = ROOT / "data" / "demo" / "consumption"


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="сквозная проверка расхода материалов")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="consumption-"))
    problems: list[str] = []

    base = ["consumption", "--works", str(DEMO / "works.json"), "--norms",
            str(DEMO / "norms.json"), "--writeoffs", str(DEMO / "writeoffs.csv"),
            "--purchases", str(DEMO / "purchases.csv"), "--daval", str(DEMO / "daval.csv")]

    # правила 1, 3, 4, 5, 6 — на демо-наборе (в нём намеренно есть нарушения)
    run = cli(*base, "--out", str(work / "demo"))
    if run.returncode != 2:
        problems.append(f"демо-набор: код {run.returncode}, ожидался 2 (в наборе есть ошибки)")
    for code in ("NORM_NO_SOURCE", "OVER_WITHOUT_REASON", "WRITE_OFF_WITHOUT_NORM",
                 "OVER_LIMIT", "DAVAL_DOUBLE_ACCOUNT"):
        if code not in run.stdout:
            problems.append(f"правило: находка {code} не показана на демо-наборе")
    report = json.loads((work / "demo" / "consumption.json").read_text(encoding="utf-8"))
    if report["norms"]["no_source"] != 1:
        problems.append(f"правило 1: норм без источника {report['norms']['no_source']}, "
                        f"ожидалась 1")
    # правило 2: без человека — справочно
    if report["totals"]["need_confirmed_rub"] != 0:
        problems.append("правило 2: без подтверждения норм человеком появилась подтверждённая "
                        "потребность")
    if report["totals"]["need_reference_rub"] <= 0:
        problems.append("правило 2: справочная потребность не посчитана")
    # правило 3: перерасход с причиной — предупреждение, без причины — ошибка
    levels = {c["code"]: c["level"] for c in report["checks"]}
    if levels.get("OVER_WITH_REASON") != "warning":
        problems.append("правило 3: перерасход с причиной должен быть предупреждением")
    if levels.get("OVER_WITHOUT_REASON") != "error":
        problems.append("правило 3: перерасход без причины должен быть ошибкой")

    # правило 2 (продолжение): человек принимает нормы
    confirmed = cli(*base, "--confirm-norms", "--by", "Иванов И.И.", "--role", "инженер ПТО",
                    "--out", str(work / "confirmed"))
    if "нормы приняты для расчёта человеком" not in confirmed.stdout:
        problems.append("правило 2: принятие норм человеком не отражено в выводе")
    confirmed_report = json.loads((work / "confirmed" / "consumption.json").read_text(
        encoding="utf-8"))
    if confirmed_report["totals"]["need_confirmed_rub"] <= 0:
        problems.append("правило 2: после принятия норм человеком подтверждённая потребность "
                        "осталась нулевой")

    # анонимное принятие норм — отказ
    anonymous = cli(*base, "--confirm-norms", "--out", str(work / "anon"))
    if anonymous.returncode != 2 or "норму принимает человек" not in anonymous.stderr:
        problems.append("правило 2: анонимное принятие норм не остановлено")

    summary = {
        "правил": 6,
        "нарушений": len(problems),
        "материалов": report["totals"]["materials"],
        "норм": report["norms"],
        "потребность_справочная_руб": report["totals"]["need_reference_rub"],
        "потребность_подтверждённая_руб": confirmed_report["totals"]["need_confirmed_rub"],
        "перерасходов_сверх_нормы": report["totals"]["over_norms"],
        "перерасход_руб": report["totals"]["over_norms_rub"],
        "превышений_лимита": report["totals"]["over_limit"],
        "остаток_давальческих": report["totals"]["daval_remainder"],
        "ошибок": report["totals"]["errors"],
        "предупреждений": report["totals"]["warnings"],
    }
    if args.json:
        print(json.dumps({**summary, "problems": problems}, ensure_ascii=False, indent=1))
    else:
        print(f"правил: 6 · материалов: {summary['материалов']} · норм применено "
              f"{summary['норм']['applied']} из {summary['норм']['total']} "
              f"(без источника {summary['норм']['no_source']})")
        print(f"потребность: справочная {summary['потребность_справочная_руб']:,.2f} ₽ · "
              f"подтверждённая человеком {summary['потребность_подтверждённая_руб']:,.2f} ₽")
        print(f"перерасходов сверх нормы: {summary['перерасходов_сверх_нормы']} на "
              f"{summary['перерасход_руб']:,.2f} ₽ · превышений лимита: "
              f"{summary['превышений_лимита']} · остаток давальческих: "
              f"{summary['остаток_давальческих']}")
        print(f"нарушений: {len(problems)}")
        for problem in problems:
            print(f"  ✗ {problem}")

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
