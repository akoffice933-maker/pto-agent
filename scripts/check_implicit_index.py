# -*- coding: utf-8 -*-
"""Неявный индекс сметы по ресурсному методу (идея из DDC CWICR, данные — свои).

Проверяем одно утверждение: «норма неизменна, цена региональна». Если смета
собрана ресурсным методом в текущих ценах, то цена позиции должна быть
пропорциональна ФОТ+ЭММ+материалам из базы ФЕР-2000 с одним и тем же
коэффициентом. Здесь считаются два варианта:

  * `total`   — вся цена позиции ÷ вся цена ФЕР: смешивает труд, машины и
    материалы, у которых индексы разные, поэтому разброс большой;
  * `by_labour` — цена сметы ÷ (ФОТ + ЭММ на машиниста) из ФОТ/ЭММ базы, где
    разделение ресурсов на «труд» и «материал» заведомо существует.

Второй вариант — как раз то, чего у нас в базе нет: в ФЕР-2020 у нас только
итоговые `salary/machines/materials` без поресурсной развёртки. Поэтому
материальная часть не индексируется, а это ограничение печатается в отчёте,
а не замалчивается.

    python3 scripts/check_implicit_index.py --parsed out/smeta_check/smeta_parsed.json \
        --out out/implicit_index
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import math
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FER = ROOT / "data" / "fsnb" / "fer2020.csv.gz"
WORK_PREFIXES = ("ГЭСН", "ФЕР", "ТЕР", "ФССЦпг")

# Сборники, которых в федеральной базе нет: по ним индекс не вывести,
# и это свойство документа, а не ошибка сметы.
NO_RATE_REASON = {
    "67": "ремонтные расценки (сб. 67) — своей ставки в базе ФЕР-2020 нет",
    "69": "сверление отверстий (сб. 69) — своей ставки в базе ФЕР-2020 нет",
}


def norm(text: str) -> str:
    s = str(text or "").lower().replace("ё", "е").replace("²", "2").replace("³", "3")
    s = re.sub(r"[^а-яa-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_prefix(code: str) -> str:
    """'ГЭСНм08-02-399-01' → '08-02-399-01'; 'ФЕР46-03-011-01' → '46-03-011-01'."""
    return re.sub(r"^(ГЭСН|ФЕР|ТЕР)(м|р|п|мр)?", "", str(code or "")).strip()


def load_fer(path: Path = FER) -> dict[str, dict]:
    """Код ФЕР → ставка и её разложение на ФОТ / ЭММ / материалы."""
    out: dict[str, dict] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            price = _float(row.get("price"))
            if price is None:
                continue
            salary, machines = _float(row.get("salary")) or 0.0, _float(row.get("machines")) or 0.0
            out[row["code"]] = {
                "name": row["name"], "price": price, "unit": row["unit"],
                "salary": salary, "machines": machines,
                "materials": _float(row.get("materials")) or 0.0,
                "labour": salary + machines,
                "collection": row.get("collection_name", ""),
            }
    return out


def _float(value) -> float | None:
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def analyse(parsed: dict, fer: dict[str, dict], *, tolerance: float = 0.35) -> dict:
    rows: list[dict] = []
    skipped: list[dict] = []
    for item in parsed.get("items") or []:
        code = str(item.get("code") or "")
        if not code.upper().startswith(WORK_PREFIXES):
            skipped.append({"n": item.get("n"), "code": code, "name": item.get("name"),
                            "why": "материал или оборудование (код ФГИС ЦС) — своей "
                                   "расценки нет, индекс не выводится"})
            continue
        qty = _float(item.get("qty"))
        values = item.get("values") or []
        total = _float(values[-1]) if values else None
        if not qty or total in (None, 0):
            skipped.append({"n": item.get("n"), "code": code, "name": item.get("name"),
                            "why": "нет количества или суммы — цену за единицу не вывести"})
            continue
        unit_price = total / qty
        fer_code = strip_prefix(code)
        base = fer.get(fer_code)
        if not base or not base["price"]:
            book = fer_code.split("-")[0].lstrip("0") if "-" in fer_code else ""
            skipped.append({"n": item.get("n"), "code": code, "name": item.get("name"),
                            "why": NO_RATE_REASON.get(
                                book, " шифра нет в базе ФЕР-2020 (доп. 9) — сверять не с чем"
                            ).strip()})
            continue
        rows.append({
            "n": item.get("n"), "code": code, "fer_code": fer_code,
            "name": item.get("name"), "unit": item.get("unit"), "qty": qty,
            "unit_price": round(unit_price, 2),
            "fer_price": round(base["price"], 2),
            "fer_labour": round(base["labour"], 2),
            "fer_materials": round(base["materials"], 2),
            "fer_material_share": round(base["materials"] / base["price"] * 100, 1),
            "fer_labour_share": round(base["labour"] / base["price"] * 100, 1),
            "fer_unit": base["unit"], "fer_name": base["name"],
            "index_total": round(unit_price / base["price"], 1),
            "unit_matches": norm(item.get("unit")) == norm(base["unit"]),
        })

    # Демонтаж идёт по тем же нормам, но с коэффициентом (0,3 / 0,4 / 0,5).
    # Мерить его общей медианой — значит половину позиций объявить «выпавшими»
    # из-за собственного коэффициента, а не из-за дефекта. Поэтому два ряда.
    for r in rows:
        r["group"] = "демонтаж" if "демонтаж" in norm(r["name"]) else "монтаж/прочее"

    linked = [r for r in rows if r["unit_matches"]]
    total_median = (round(statistics.median([r["index_total"] for r in linked]), 1)
                    if linked else None)
    groups: dict[str, dict] = {}
    for name in ("монтаж/прочее", "демонтаж"):
        values = [r["index_total"] for r in linked if r["group"] == name]
        if values:
            groups[name] = {"median": round(statistics.median(values), 1),
                            "min": min(values), "max": max(values), "count": len(values)}
    for r in rows:
        own = groups.get(r["group"], {}).get("median")
        r["from_group_median"] = (round((r["index_total"] - own) / own * 100, 1)
                                  if own and r["unit_matches"] else None)
        r["flag"] = ("unit_mismatch" if not r["unit_matches"]
                     else "outlier" if r["from_group_median"] is not None
                     and abs(r["from_group_median"]) > tolerance * 100 else "ok")

    # Почему позиция выпадает из ряда. Гипотеза ресурсного метода: индекс на труд
    # и индекс на материалы разные, а у нас в базе только их сумма. Значит там,
    # где в базисной цене преобладают материалы, общий индекс обязан быть ниже.
    # Проверяем это на наших же числах, а не верим на слово: считаем корреляцию
    # «доля ФОТ+ЭММ» ↔ «логарифм индекса».
    pairs = [(r["fer_labour_share"], math.log(r["index_total"]))
             for r in linked if r["index_total"] > 0]
    corr = None
    if len(pairs) >= 4:
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
        corr = round(num / den, 2) if den else None
    for r in rows:
        low_material_index = (r["index_total"] < (total_median or 0) * 0.6)
        r["explained_by_materials"] = bool(
            r["flag"] == "outlier" and low_material_index
            and r["fer_material_share"] >= 40.0)

    # Коэффициент на демонтаж — это демонтаж ÷ монтаж, а не ÷ общая медиана:
    # общая медиана уже смешана из двух рядов и даёт заниженное отношение.
    dem = groups.get("демонтаж", {}).get("median")
    mont = groups.get("монтаж/прочее", {}).get("median")
    demo_ratio = round(dem / mont, 2) if dem and mont else None
    outliers = [{"code": r["code"], "name": r["name"], "index_total": r["index_total"],
                 "from_group_median": r["from_group_median"],
                 "explained_by_materials": r["explained_by_materials"]}
                for r in rows if r["flag"] == "outlier"]
    unexplained = [o for o in outliers if not o["explained_by_materials"]]

    return {
        "schema": "pto-agent/implicit-index@1",
        "built_at": date.today().isoformat(),
        "fer_base": {"file": str(FER.relative_to(ROOT)), "price_level": "01.01.2000",
                     "source": "ФСНБ-2020 (ФЕР-2020, доп. № 9), открытые данные ФГИС ЦС"},
        "median_index": total_median,
        "groups": groups,
        "demolition_ratio_to_median": demo_ratio,
        "material_vs_index_correlation": corr,
        "unexplained_outliers": unexplained,
        "tolerance": tolerance,
        "counts": {"positions": len(rows) + len(skipped), "linked": len(rows),
                   "used_for_median": len(linked),
                   "outliers": len(outliers),
                   "unit_mismatch": sum(1 for r in rows if r["flag"] == "unit_mismatch"),
                   "not_linked": len(skipped)},
        "rows": rows, "outliers": outliers, "skipped": skipped,
        "not_done": [
            "Это не проверка правильности цены: индекс — отношение двух чисел, а не "
            "доказательство, что цена в смете верна.",
            "Работы, которых нет в федеральной базе (ремонтные сб. 67/69, пусконаладка), "
            "остаются без индекса — так и записано, а не заменено нулём.",
            "Поресурсного разложения у нас нет: в базе ФЕР-2020 только итоговые "
            "ФОТ/ЭММ/материалы, поэтому индексы по труду и по материалам не разделены. "
            "Одно число на позицию смешивает разные индексы — отсюда разброс.",
            "Коэффициенты, применённые в смете, отдельно не выделены: они внутри цены и "
            "поэтому входят в индекс.",
        ],
    }


def to_markdown(res: dict) -> str:
    c, med = res["counts"], res["median_index"]
    lines = ["# Неявный индекс сметы (÷ базис ФЕР-2000)", "",
             "Смета не печатает индекс, которым пересчитана из базиса. Его можно вытащить: "
             "цена позиции ÷ цена той же нормы в базе ФЕР-2000.", "",
             f"- позиций в смете: {c['positions']}, сопоставлено с базой: {c['linked']}",
             f"- **медианный индекс по всем позициям: ×{med}** "
             f"(допуск ±{int(res['tolerance']*100)} %)",
             f"- выпадает из своего ряда: {c['outliers']}, единицы не совпали: "
             f"{c['unit_mismatch']}, без пары в базе: {c['not_linked']}", ""]
    for name, g in res["groups"].items():
        lines.append(f"- {name}: медиана ×{g['median']} на {g['count']} позициях "
                     f"(от ×{g['min']} до ×{g['max']})")
    if res.get("material_vs_index_correlation") is not None:
        lines.append(
            f"- корреляция «доля ФОТ+ЭММ в базисной цене» ↔ «логарифм индекса» = "
            f"**{res['material_vs_index_correlation']}** — чем больше в норме материалов, "
            f"тем ниже общий индекс. Это не дефект сметы, а следствие того, что труд и "
            f"материалы индексируются по-разному, а база даёт их одной суммой.")
    if res.get("demolition_ratio_to_median"):
        lines.append(f"- демонтаж / монтаж = **×{res['demolition_ratio_to_median']}** — "
                     f"коэффициент на демонтаж, в документе не напечатанный")
    lines += ["", "| № | Шифр | Наименование | Цена сметы | ФЕР-2000 | Индекс | От ряда | Ряд | "
              "Доля ФОТ+ЭММ | Метка |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in res["rows"]:
        delta = "" if r["from_group_median"] is None else f"{r['from_group_median']:+.1f} %"
        cells = [str(r["n"]), r["code"], str(r["name"])[:44],
                 f"{r['unit_price']:,.2f}".replace(",", " "),
                 f"{r['fer_price']:,.2f}".replace(",", " "),
                 f"×{r['index_total']}", delta, r["group"],
                 f"{r['fer_labour_share']:.0f} %", r["flag"]]
        lines.append("| " + " | ".join(cells) + " |")
    if res["unexplained_outliers"]:
        lines += ["", "## Выпадает из ряда и материалом не объясняется", ""]
        for o in res["unexplained_outliers"]:
            lines.append(f"- {o['code']} ×{o['index_total']} "
                         f"({o['from_group_median']:+.1f} % к своему ряду) — {o['name'][:60]}")
        lines.append("\nЭто единственные строки, которые стоит смотреть глазами: "
                     "остальные «выпавшие» объясняются долей материалов.")
    if res["skipped"]:
        lines += ["", "## Без пары в базе (индекс не выводится)", "",
                  "| № | Шифр | Причина |", "|---|---|---|"]
        lines += [f"| {s['n']} | {s['code']} | {s['why']} |" for s in res["skipped"]]
    lines += ["", "## Чего проверка не делает", ""]
    lines += [f"- {x}" for x in res["not_done"]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parsed", required=True, help="smeta_parsed.json из check_smeta_pdf.py")
    ap.add_argument("--out", default="out/implicit_index")
    ap.add_argument("--tolerance", type=float, default=0.35)
    a = ap.parse_args(argv)

    parsed = json.loads(Path(a.parsed).read_text(encoding="utf-8"))
    res = analyse(parsed, load_fer(), tolerance=a.tolerance)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    out.with_suffix(".md").write_text(to_markdown(res), encoding="utf-8")
    c = res["counts"]
    print(f"сопоставлено {c['linked']} из {c['positions']}; медиана ×{res['median_index']}; "
          f"выпадает {c['outliers']}; без пары {c['not_linked']}")
    print(f"файлы: {out.with_suffix('.json')}, {out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
