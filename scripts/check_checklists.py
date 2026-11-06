#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сквозная проверка чек-листов на настоящем CLI: 6 правил, код 2 при нарушении.

    python scripts/check_checklists.py [--keep] [--json]

Правила:

1. пункт без источника (документ + пункт) в чек-лист не попадает;
2. чек-лист с непроверенными пунктами подписать нельзя;
3. «не соответствует» без описания не подписывается;
4. без ФИО и роли подписи нет;
5. после подписи правка результатов аннулирует подпись (чек-лист «изменён после подписи»);
6. при несоответствиях отчёт не говорит «работы можно предъявлять».
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="сквозная проверка чек-листов")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="checklists-"))
    out = work / "cl"
    problems: list[str] = []

    # правило 1 — пункт без источника
    bad_items = work / "bad_items.json"
    bad_items.write_text(json.dumps([
        {"requirement": "Проверить всё по проекту"},
        {"requirement": "Допуск по вертикали", "source": {"doc": "СП 70.13330.2012"}},
    ], ensure_ascii=False), encoding="utf-8")
    bad = cli("checklist", "--out", str(out), "build", "--items", str(bad_items))
    if bad.returncode != 2:
        problems.append(f"правило 1: код {bad.returncode}, ожидался 2 (пункты без источника)")
    if "ITEM_WITHOUT_SOURCE" not in bad.stdout:
        problems.append("правило 1: находка ITEM_WITHOUT_SOURCE не показана")
    bad_doc = json.loads((out / "checklist_объект.json").read_text(encoding="utf-8"))
    if bad_doc["items"] or bad_doc["totals"]["rejected"] != 2:
        problems.append("правило 1: пункты без источника попали в чек-лист")
    if bad_doc["can_close"]["allowed"]:
        problems.append("правило 1: пустой чек-лист признан годным для предъявления работ")

    # чек-лист по виду работ
    built = cli("checklist", "--out", str(out), "build", "--work-type", "earthworks",
                "--object-name", "Проверочный объект", "--number", "ЧЛ-1",
                "--act-ref", "КС-2 №38")
    if built.returncode != 0:
        problems.append(f"сборка чек-листа: код {built.returncode}\n{built.stderr[-400:]}")
    path = out / "checklist_ЧЛ-1.json"
    checklist = json.loads(path.read_text(encoding="utf-8"))
    if checklist["totals"]["items"] < 2:
        problems.append("в чек-листе по виду работ меньше двух пунктов — проверять нечего")

    # правило 2 — подпись при непроверенных пунктах
    early = cli("checklist", "--out", str(out), "sign", "--checklist", str(path),
                "--by", "Иванов И.И.", "--role", "начальник ПТО")
    if early.returncode != 2 or "не проверено пунктов" not in early.stdout:
        problems.append("правило 2: чек-лист с непроверенными пунктами удалось подписать")

    # правило 3 — несоответствие без описания
    no_note = work / "no_note.json"
    no_note.write_text(json.dumps([{"no": item["no"], "result": "не соответствует"}
                                   for item in checklist["items"]], ensure_ascii=False),
                       encoding="utf-8")
    cli("checklist", "--out", str(out), "fill", "--checklist", str(path),
        "--results", str(no_note), "--by", "Петров П.П.")
    refused = cli("checklist", "--out", str(out), "sign", "--checklist", str(path),
                  "--by", "Иванов И.И.", "--role", "начальник ПТО")
    if refused.returncode != 2 or "несоответствия без описания" not in refused.stdout:
        problems.append("правило 3: несоответствие без описания удалось подписать")

    # правило 4 — без ФИО подписи нет
    anonymous = cli("checklist", "--out", str(out), "sign", "--checklist", str(path),
                    "--by", " ", "--role", " ")
    if anonymous.returncode != 2 or "ПОДПИСЬ НЕ ПРИНЯТА" not in anonymous.stdout:
        problems.append("правило 4: анонимная подпись не остановлена")

    # подписываем корректный чек-лист с одним несоответствием и описанием
    with_note = work / "with_note.json"
    first = checklist["items"][0]["no"]
    with_note.write_text(json.dumps([
        {"no": item["no"], "result": "не соответствует" if item["no"] == first else "соответствует",
         "note": "коэффициент уплотнения 0,92 при требуемом 0,95 — требуется доп. уплотнение"
                 if item["no"] == first else ""}
        for item in checklist["items"]], ensure_ascii=False), encoding="utf-8")
    cli("checklist", "--out", str(out), "fill", "--checklist", str(path),
        "--results", str(with_note), "--by", "Петров П.П.")
    signed = cli("checklist", "--out", str(out), "sign", "--checklist", str(path),
                 "--by", "Иванов И.И.", "--role", "начальник ПТО", "--reason", "проверено")
    if signed.returncode != 0:
        problems.append(f"подпись корректного чек-листа не прошла:\n{signed.stdout[-500:]}")
    # правило 6 — при несоответствии работы не предъявляются
    if "работы предъявлять нельзя" not in signed.stdout:
        problems.append("правило 6: чек-лист с несоответствием признан готовым к предъявлению")
    signed_doc = json.loads(path.read_text(encoding="utf-8"))
    if signed_doc["can_close"]["allowed"]:
        problems.append("правило 6: can_close разрешил предъявление при несоответствии")
    markdown = (out / "checklist_ЧЛ-1.md").read_text(encoding="utf-8").lower()
    if "работы приняты" in markdown or "готова к сдаче" in markdown:
        problems.append("правило 6: в отчёте утверждение о приёмке работ")

    # правило 5 — правка после подписи аннулирует подпись
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["items"][0]["result"] = "соответствует"
    tampered["items"][0]["note"] = ""
    tampered_path = out / "checklist_ЧЛ-1.json"
    tampered_path.write_text(json.dumps(tampered, ensure_ascii=False, indent=1), encoding="utf-8")
    after = cli("checklist", "--out", str(out), "fill", "--checklist", str(tampered_path),
                "--results", str(_empty(work)),
                "--by", "Иванов И.И.")
    if "недействительна" not in after.stdout:
        problems.append("правило 5: правка после подписи не аннулировала подпись")

    summary = {
        "правил": 6,
        "нарушений": len(problems),
        "пунктов_в_чек_листе": checklist["totals"]["items"],
        "отклонено_без_источника": bad_doc["totals"]["rejected"],
        "источников_сверено": checklist["totals"]["source_verified"],
        "статус_после_подписи": signed_doc["status"],
        "работы_можно_предъявлять": signed_doc["can_close"]["allowed"],
    }
    if args.json:
        print(json.dumps({**summary, "problems": problems}, ensure_ascii=False, indent=1))
    else:
        print(f"правил: 6 · пунктов в чек-листе: {summary['пунктов_в_чек_листе']} · "
              f"отклонено без источника: {summary['отклонено_без_источника']} · источников "
              f"сверено: {summary['источников_сверено']}")
        print(f"статус после подписи: {summary['статус_после_подписи']} · работы можно "
              f"предъявлять: {'да' if summary['работы_можно_предъявлять'] else 'нет'}")
        print(f"нарушений: {len(problems)}")
        for problem in problems:
            print(f"  ✗ {problem}")

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 2 if problems else 0


def _empty(work: Path) -> Path:
    path = work / "empty.json"
    path.write_text("[]", encoding="utf-8")
    return path


if __name__ == "__main__":
    sys.exit(main())
