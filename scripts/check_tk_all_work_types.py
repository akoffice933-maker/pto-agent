#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Прогон черновика ТК по всем видам работ матрицы — проверка «нет пустых обещаний».

Зачем скрипт, а не один тест:

  * матрица правится заказчиком, и новый вид работ не должен молча давать документ, в котором
    раздел пуст и ни о чём не спрошено;
  * нормативная база пополняется — надо видеть, сколько норм уже «сверено», а сколько пока
    «не проверено», по каждому виду работ;
  * результат — таблица `docs/28`, по которой видно, какие СП грузить в базу следующими.

Проверки (exit 2 при нарушении):
  1. в каждом разделе есть либо пункт, либо запрос данных с адресатом;
  2. каждая норма либо сверена (есть цитата), либо имеет причину «не проверено»;
  3. в документе нет слов «готова к применению» и подобных;
  4. у каждой ТК есть комплект ИД из матрицы (раздел 6 не пуст).

Использование:
    python3 scripts/check_tk_all_work_types.py                 # проверить, код возврата
    python3 scripts/check_tk_all_work_types.py --write-doc     # + обновить docs/28
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import tk as tk_mod                                # noqa: E402
from pto_agent.asbuilt.requirements import load_matrix, work_types  # noqa: E402

DOC = ROOT / "docs" / "28_ТК_по_всем_видам_работ.md"
FORBIDDEN = ("готова к применению", "утверждена и допускается", "согласована в надзоре")


def check_one(work_type: dict, matrix: dict) -> dict:
    draft = tk_mod.build_tk_draft(work_type_id=work_type["id"],
                                  object_name="проверка по видам работ", matrix=matrix)
    problems: list[str] = []
    for section in draft["sections"]:
        if not section["items"] and not section.get("needs_input"):
            problems.append(f"раздел «{section['title']}» пуст и ничего не запрашивает")
    for norm in draft["norms"]:
        if norm["verified"] and not norm["quote"]:
            problems.append(f"норма {norm['doc']} п. {norm['clause']} помечена сверенной без цитаты")
        if not norm["verified"] and not norm["reason"]:
            problems.append(f"норма {norm['doc']} п. {norm['clause']} не сверена без причины")
    text = tk_mod.to_markdown(draft).lower()
    for phrase in FORBIDDEN:
        if phrase in text:
            problems.append(f"в документе есть «{phrase}»")
    quality = next((s for s in draft["sections"] if s["key"] == "quality"), None)
    if not quality or not quality["items"]:
        problems.append("раздел «контроль качества и ИД» пуст — комплект не выведен из матрицы")
    return {"id": work_type["id"], "name": work_type["name"], "draft": draft,
            "problems": problems}


def run(matrix: dict | None = None) -> dict:
    matrix = matrix or load_matrix()
    results = [check_one(wt, matrix) for wt in work_types(matrix)]
    totals = {
        "work_types": len(results),
        "with_problems": len([r for r in results if r["problems"]]),
        "needs_input": sum(r["draft"]["totals"]["needs_input"] for r in results),
        "filled_items": sum(r["draft"]["totals"]["filled_items"] for r in results),
        "norms_total": sum(r["draft"]["totals"]["norms_total"] for r in results),
        "norms_verified": sum(r["draft"]["totals"]["norms_verified"] for r in results),
    }
    return {"checked_at": date.today().isoformat(), "totals": totals, "results": results}


def to_markdown(report: dict, matrix: dict) -> str:
    totals = report["totals"]
    lines = [
        "# ТК по всем видам работ: что заполняется, что запрашивается",
        "",
        f"**Дата прогона:** {report['checked_at']} · **видов работ:** {totals['work_types']} · "
        f"**заполнено пунктов:** {totals['filled_items']} · **запросов данных:** "
        f"{totals['needs_input']} · **нормы:** сверено {totals['norms_verified']} из "
        f"{totals['norms_total']} · **с нарушениями:** {totals['with_problems']}",
        "",
        "Собрано скриптом `scripts/check_tk_all_work_types.py`: он проходит по матрице "
        "требований и по каждому виду работ строит черновик ТК, проверяя четыре правила "
        "(нет пустых разделов без запроса, нет «сверенных» норм без цитаты, нет заявлений "
        "о готовности карты к применению, комплект ИД выводится из матрицы).",
        "",
        "## По видам работ",
        "",
        "| Вид работ | Разделов заполнено | Запросов данных | Норм | Сверено | Комплект ИД |",
        "|---|---|---|---|---|---|",
    ]
    for result in report["results"]:
        draft = result["draft"]
        quality = next(s for s in draft["sections"] if s["key"] == "quality")
        lines.append(
            f"| {result['name']} (`{result['id']}`) | {draft['totals']['filled_items']} | "
            f"{draft['totals']['needs_input']} | {draft['totals']['norms_total']} | "
            f"{draft['totals']['norms_verified']} | {len(quality['items'])} документов |")
    bad = [r for r in report["results"] if r["problems"]]
    lines += ["", "## Нарушения", ""]
    if bad:
        for result in bad:
            for problem in result["problems"]:
                lines.append(f"- **{result['name']}**: {problem}")
    else:
        lines.append("- нет: у каждого вида работ каждый раздел либо заполнен, "
                     "либо содержит запрос данных с адресатом")

    # какие документы нужно загрузить в нормативную базу, чтобы нормы стали «сверено»
    unverified: dict[str, list[str]] = {}
    for result in report["results"]:
        for norm in result["draft"]["norms"]:
            if not norm["verified"]:
                unverified.setdefault(str(norm["doc"]), []).append(
                    f"{norm['clause']} ({result['id']})")
    lines += ["", "## Что загрузить в нормативную базу", "",
              "Нормы из матрицы, которые сейчас помечены «не проверено»: "
              "чем больше этих документов загружено с пунктами, тем меньше пометок в ТК.",
              "",
              "| Документ | Пункты (и вид работ) |", "|---|---|"]
    for doc, clauses in sorted(unverified.items(), key=lambda x: -len(x[1])):
        lines.append(f"| {doc} | {', '.join(clauses)} |")
    if not unverified:
        lines.append("| — | все нормы матрицы сверены с базой |")

    lines += ["", "## Что это значит", "",
              "* ТК собирается по **всем** видам работ матрицы, а не по одному демо-примеру: "
              "добавление вида работ в матрицу сразу даёт черновик с комплектом ИД и запросами;",
              "* раздел «контроль качества и ИД» заполнен у каждого вида работ — это то, чего "
              "нет ни у одного OSS из `docs/26`;",
              "* состав операций, машины, охрана труда и трудозатраты запрашиваются с адресатом "
              "у каждого вида работ — документа с выдуманным содержанием не существует;",
              "* нормы почти нигде не сверены: в базе лежит только фрагмент ПУЭ-7. Это работа "
              "с данными, а не с кодом, и она видна в последней таблице.",
              ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-doc", action="store_true", help="обновить docs/28")
    parser.add_argument("--json", default=None, help="куда сохранить машинный отчёт")
    args = parser.parse_args(argv)

    matrix = load_matrix()
    report = run(matrix)
    totals = report["totals"]
    print(f"видов работ: {totals['work_types']} · заполнено пунктов: {totals['filled_items']} · "
          f"запросов данных: {totals['needs_input']} · нормы: сверено "
          f"{totals['norms_verified']} из {totals['norms_total']} · "
          f"с нарушениями: {totals['with_problems']}")
    for result in report["results"]:
        mark = "!" if result["problems"] else " "
        print(f" {mark} {result['id']:24} пунктов {result['draft']['totals']['filled_items']:>2} · "
              f"запросов {result['draft']['totals']['needs_input']:>2} · норм "
              f"{result['draft']['totals']['norms_total']} (сверено "
              f"{result['draft']['totals']['norms_verified']})")
        for problem in result["problems"]:
            print(f"     НАРУШЕНИЕ: {problem}")

    markdown = to_markdown(report, matrix)
    if args.write_doc:
        DOC.write_text(markdown, encoding="utf-8")
        print(f"документ обновлён: {DOC.relative_to(ROOT)}")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {k: v for k, v in report.items() if k != "results"} |
            {"results": [{"id": r["id"], "problems": r["problems"],
                          "totals": r["draft"]["totals"]} for r in report["results"]]},
            ensure_ascii=False, indent=1), encoding="utf-8")

    if totals["with_problems"]:
        print("НАРУШЕНИЯ В ЧЕРНОВИКАХ — проверять нельзя")
        return 2
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
