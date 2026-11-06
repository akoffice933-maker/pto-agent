#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сквозная проверка реестра ТК на настоящем CLI: 5 правил, код 2 при нарушении.

Прогоняется в CI отдельным шагом. Тесты проверяют функции по отдельности, а этот скрипт —
то, что увидит инженер: команды, файлы в каталоге и код возврата.

    python scripts/check_tk_registry.py [--keep] [--json]

Правила:

1. две карты одного объекта не затирают друг друга (число файлов = числу карт);
2. свежий черновик — «не проверен», а не «готово»;
3. подтверждение с ФИО и ролью даёт «проверен инженером» и дату;
4. правка разделов после проверки аннулирует проверку;
5. в отчёте нет утверждений о готовности/утверждении.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("готова к применению", "готов к применению", "согласована", "допущена к работе")


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pto_agent", *argv], cwd=ROOT,
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="сквозная проверка реестра ТК")
    ap.add_argument("--keep", action="store_true", help="не удалять рабочий каталог")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="tk-registry-"))
    out = work / "tk"
    problems: list[str] = []

    def run(*argv: str, expect: int = 0) -> subprocess.CompletedProcess:
        result = cli(*argv)
        if result.returncode != expect:
            problems.append(f"{' '.join(argv[:4])}: код {result.returncode}, ожидался {expect}\n"
                            f"{result.stdout[-600:]}{result.stderr[-400:]}")
        return result

    # три карты по двум объектам
    for work_type in ("earthworks", "concrete", "electrical"):
        run("tk", "--out", str(out), "draft", "--work-type", work_type,
            "--object-name", "Школа на 550 мест")
    run("tk", "--out", str(out), "ppr", "--object-name", "Детский сад на 220 мест")

    # правило 1
    drafts = sorted(out.glob("tk_*.json")) + sorted(out.glob("ppr_*.json"))
    drafts = [p for p in drafts if p.name != "registry.json"]
    if len(drafts) != 4:
        problems.append(f"правило 1: файлов черновиков {len(drafts)}, ожидалось 4 — "
                        f"карты затирают друг друга: {[p.name for p in drafts]}")

    # правило 2
    registry = run("tk", "--out", str(out), "registry")
    if "проверено инженером 0" not in registry.stdout:
        problems.append("правило 2: свежие черновики посчитаны проверенными")
    data = json.loads((out / "registry.json").read_text(encoding="utf-8"))
    if data["totals"]["confirmed"] != 0 or data["totals"]["not_checked"] != 4:
        problems.append(f"правило 2: ожидалось 0 проверенных и 4 непроверенных, "
                        f"получено {data['totals']}")
    run("tk", "--out", str(out), "registry", "--strict", expect=2)

    # правило 3
    run("tk", "--out", str(out), "confirm", "--draft", str(out / "tk_concrete.json"),
        "--by", "Иванов И.И.", "--role", "главный инженер", "--reason", "проверка комплектности")
    run("tk", "--out", str(out), "registry")           # пересобрать реестр после подтверждения
    data = json.loads((out / "registry.json").read_text(encoding="utf-8"))
    concrete = next(e for slot in data["objects"] for e in slot["drafts"]
                    if e["file"] == "tk_concrete.json")
    if not (concrete["check_valid"] and concrete["by"] == "Иванов И.И."
            and concrete["role"] == "главный инженер" and concrete["confirmed_at"]):
        problems.append(f"правило 3: подтверждение не отразилось в реестре: {concrete['status']}")

    # правило 4
    draft_path = out / "tk_concrete.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["sections"][1]["items"].append({"text": "правка после проверки", "source": "кто-то"})
    draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=1), encoding="utf-8")
    changed = run("tk", "--out", str(out), "registry", expect=2)
    if "проверка недействительна" not in changed.stdout:
        problems.append("правило 4: правка после проверки не аннулировала проверку")

    # правило 5
    text = (out / "registry.md").read_text(encoding="utf-8").lower()
    for phrase in FORBIDDEN:
        if phrase in text:
            problems.append(f"правило 5: в отчёте утверждение о готовности «{phrase}»")
    if "не право применять" not in text:
        problems.append("правило 5: в отчёте нет оговорки о том, что реестр не даёт права "
                        "применять карты")

    state = json.loads((out / "registry.json").read_text(encoding="utf-8"))
    summary = {
        "правил": 5,
        "нарушений": len(problems),
        "объектов": state["totals"]["objects"],
        "черновиков": state["totals"]["drafts"],
        "проверено_инженером": state["totals"]["confirmed"],
        "изменено_после_проверки": state["totals"]["changed_after_confirm"],
        "запросов_данных": state["totals"]["needs_input"],
        "правила": ["файлы не затираются", "черновик — не проверен", "проверка с ФИО и датой",
                    "правка аннулирует проверку", "нет утверждений о готовности"],
    }
    if args.json:
        print(json.dumps({**summary, "problems": problems}, ensure_ascii=False, indent=1))
    else:
        print(f"правил: {summary['правил']} · объектов: {summary['объектов']} · "
              f"черновиков: {summary['черновиков']} · проверено инженером: "
              f"{summary['проверено_инженером']} · изменено после проверки: "
              f"{summary['изменено_после_проверки']} · запросов данных: "
              f"{summary['запросов_данных']}")
        print(f"нарушений: {len(problems)}")
        for problem in problems:
            print(f"  ✗ {problem}")

    if not args.keep:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    return 2 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
