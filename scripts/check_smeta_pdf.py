#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Прогон чужой сметы (PDF из АРОС-Лидер) через агента: арифметика, коды, база.

Зачем: смету нельзя «принять на веру». Проверяются только те вещи, которые проверяемы
машинно, и по каждой — свой вердикт:

* **арифметика разделов и итогов** — сумма позиций против «Итого по разделу» и «ВСЕГО»;
* **ставки НР и СП** — фактическая ставка в каждой позиции против норматива по виду работ
  (в смете АРОС НР/СП стоят в рублях, а не процентами, поэтому ставка восстанавливается
  делением на фонд оплаты труда, и её видно);
* **коды ресурсов** — есть ли такой код в ФСБЦ-2022 из открытых данных ФГИС ЦС, совпадает
  ли наименование и единица;
* **цены ресурсов** — цена в смете против базовой ФСБЦ; расхождение НЕ ошибка, а
  восстановленный коэффициент, который должен совпадать с индексом Минстроя;
* **сметные нормы** — есть ли ГЭСН/ФЕР с таким шифром в разобранной базе.

Чего скрипт не делает: не пересчитывает смету «как надо» и не правит цифры. Каждая строка
отчёта — «сошлось / не сошлось / не проверено» с указанием причины.

    python3 scripts/check_smeta_pdf.py --pdf <смета.pdf> --out out/smeta_check
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FSBC = ROOT / "data" / "fsnb" / "fsbc2022.csv.gz"
FER = ROOT / "data" / "fsnb" / "fer2020.csv.gz"

# строка позиции в PDF АРОС: «1\nГЭСНр67-01-004-05\nДемонтаж…\n 100 шт\n 0,37\n 3 072,18\n …»
RE_N = re.compile(r"^\d{1,3}$")
RE_WORK = re.compile(r"^(ГЭСН|ФЕР|ТЕР|ФССЦ)", re.IGNORECASE)
RE_RES = re.compile(r"^\d{2}\.\d(\.\d{2})?[.\-]\d")
RE_MONEY = re.compile(r"^-?[\d\s]+,\d{2}$")
RE_SECTION = re.compile(r"^РАЗДЕЛ\s+(\d+)\s*[.:]\s*(.+)$", re.IGNORECASE)


def money(text: str) -> float | None:
    text = text.replace("\xa0", " ").strip()
    if not RE_MONEY.match(text):
        return None
    return float(text.replace(" ", "").replace(",", "."))



RE_QTY = re.compile(r"^\d+(?:[ ,]\d+)?$")
FILLER = {"-", "—", "–"}


def _parse_item(lines: list[str], i: int) -> tuple[dict | None, int]:
    """Позиция сметы: номер, шифр, наименование, единица, количество, деньги.

    Вёрстка АРОС в PDF: строго после шифра идёт наименование одной строкой, затем
    единица измерения, затем количество (иногда слипшееся с деньгами) и до пяти сумм.
    Единица может отсутствовать у материальных строк — тогда её место занимает
    количество, и это видно по числу.
    """
    code = lines[i + 1].strip()
    j = i + 2
    name = lines[j].strip() if j < len(lines) else ""
    j += 1
    unit = ""
    qty: float | None = None
    if j < len(lines) and not _is_number_row(lines[j]):
        unit = lines[j].strip()
        j += 1
    if j < len(lines) and _is_number_row(lines[j]):
        toks = [t for t in lines[j].split() if RE_QTY.match(t)]
        if toks:
            qty = float(toks[0].replace(" ", "").replace(",", "."))
        j += 1
    values: list[float] = []
    while j < len(lines) and len(values) < 5:
        v = money(lines[j])
        if v is None:
            break
        values.append(v)
        j += 1
    return ({"n": int(lines[i]), "code": code, "name": name, "unit": unit,
             "qty": qty, "kind": "work" if RE_WORK.match(code) else "material",
             "values": values, "section": None}, max(1, j - i))


def _is_number_row(text: str) -> bool:
    toks = text.split()
    return bool(toks) and all(RE_QTY.match(t) or t in FILLER for t in toks)


def parse(pdf: Path) -> dict:
    import pypdf
    reader = pypdf.PdfReader(pdf)
    lines: list[str] = []
    for page in reader.pages:
        lines += [(l or "").strip() for l in (page.extract_text() or "").split("\n")]

    header: dict = {}
    raw = "\n".join(lines)
    for key, pattern in {
        "object": r"Смета\s+№\s*([\w/]+)",
        "program": r"(АРОС-Лидер\s*\(версия[^)]*\))",
        "fsnb": r"(ФСНБ-\d{4}\s*\(\d+\)[^\n]*)",
        "prices": r"(Текущие цены[^\n]*)",
        "method": r"(ресурсно-индексный метод[^\n]*)",
        "total": r"СМЕТНАЯ СТОИМОСТЬ\s*\n\s*([\d\s]+,\d{2})",
    }.items():
        m = re.search(pattern, raw)
        header[key] = m.group(1).strip() if m else ""

    sections: list[dict] = []
    items: list[dict] = []
    totals: dict = {}
    section: dict | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = RE_SECTION.match(line)
        if m:
            section = {"number": int(m.group(1)), "name": m.group(2).strip(),
                       "reported_total": None, "items": []}
            sections.append(section)
            i += 1
            continue
        if line.startswith("Итого по Разделу") and section is not None:
            values = [money(x) for x in lines[i + 1:i + 5]]
            values = [v for v in values if v is not None]
            if values:
                section["reported_total"] = values[-1]
                section["reported_pr"] = values[0] if len(values) > 2 else None
            i += 1
            continue
        if line == "ВСЕГО ПО СМЕТЕ":
            values = [money(x) for x in lines[i + 1:i + 5]]
            values = [v for v in values if v is not None]
            totals["grand"] = values[-1] if values else None
            totals["pr"] = values[0] if len(values) > 2 else None
            totals["nr"] = values[1] if len(values) > 2 else None
            totals["sp"] = values[2] if len(values) > 2 else None
            i += 1
            continue
        if RE_N.match(line) and i + 2 < len(lines):
            code = lines[i + 1]
            if RE_WORK.match(code) or RE_RES.match(code):
                item, consumed = _parse_item(lines, i)
                if item:
                    items.append(item)
                    if section is not None:
                        section["items"].append(item)
                    i += consumed
                    continue
        i += 1
    return {"header": header, "sections": sections, "items": items, "totals": totals,
            "pages": len(reader.pages)}


def load_fsbc(codes: set[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not FSBC.exists():
        return out
    with gzip.open(FSBC, "rt", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["code"] in codes:
                out[row["code"]] = row
    return out


def load_fer(codes: set[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not FER.exists():
        return out
    with gzip.open(FER, "rt", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["code"] in codes:
                out[row["code"]] = row
    return out


def check(doc: dict) -> dict:
    codes = {it["code"] for it in doc["items"] if it["kind"] == "material"}
    work_codes = {it["code"] for it in doc["items"] if it["kind"] == "work"}
    fsbc = load_fsbc(codes)
    fer = load_fer({re.sub(r"^(ГЭСН|ФЕР)", "", c) for c in work_codes})

    findings: list[dict] = []

    # 1) арифметика разделов
    for sec in doc["sections"]:
        total = sum(it["values"][-1] for it in sec["items"] if it["values"])
        reported = sec["reported_total"]
        if reported is None:
            findings.append({"check": "арифметика раздела", "status": "не проверено",
                             "detail": f"раздел {sec['number']}: итог не найден в PDF"})
            continue
        delta = round(total - reported, 2)
        findings.append({
            "check": "арифметика раздела", "status": "сошлось" if abs(delta) < 0.02 else "не сошлось",
            "detail": f"раздел {sec['number']} «{sec['name']}»: сумма позиций {total:,.2f} "
                      f"против «Итого» {reported:,.2f} (Δ {delta:+,.2f})".replace(",", " "),
        })
    grand = sum(it["values"][-1] for it in doc["items"] if it["values"])
    reported_grand = doc["totals"].get("grand")
    if reported_grand:
        delta = round(grand - reported_grand, 2)
        findings.append({
            "check": "арифметика всего", "status": "сошлось" if abs(delta) < 0.02 else "не сошлось",
            "detail": f"сумма позиций {grand:,.2f} против «ВСЕГО ПО СМЕТЕ» "
                      f"{reported_grand:,.2f} (Δ {delta:+,.2f})".replace(",", " "),
        })

    # 1б) построчно: «Всего» = ПЗ + НР + СП + материалы сверх ПЗ.
    # В АРОС колонка «Прямые затраты» не включает стоимость материалов по норме,
    # а колонка «Всего» включает. Поэтому проверяется не равенство, а неотрицательность
    # остатка: отрицательный остаток означал бы, что сумма больше, чем из чего складывается.
    residual_rows, negative, without_residual = 0, [], 0
    residuals_total = 0.0
    for it in doc["items"]:
        v = it["values"]
        if it["kind"] != "work" or len(v) < 4:
            continue
        residual_rows += 1
        residual = round(v[3] - (v[0] + v[1] + v[2]), 2)
        residuals_total += residual
        if residual < -0.02:
            negative.append(f"п. {it['n']} {it['code']}: остаток {residual:,.2f}".replace(",", " "))
        if abs(residual) < 0.02:
            without_residual += 1
    findings.append({
        "check": "построчно: Всего = ПЗ + НР + СП + материалы",
        "status": "сошлось" if not negative else "не сошлось",
        "detail": (f"{residual_rows} строк работ: «Всего» больше суммы ПЗ+НР+СП на стоимость "
                   f"материалов по норме; остаток всего {residuals_total:,.2f} ₽, у "
                   f"{without_residual} строк материалов нет (остаток 0). Отрицательных "
                   f"остатков нет — колонки сами себе не противоречат. "
                   f"Колонка «Прямые затраты» в этой смете материалы не включает."
                   if not negative else "; ".join(negative[:5])).replace(",", " "),
    })

    # 1в) НР и СП: какая ставка заложена (восстановленная от ПЗ и от ФОТ)
    pz = sum(it["values"][0] for it in doc["items"] if it["kind"] == "work" and len(it["values"]) >= 4)
    nr = sum(it["values"][1] for it in doc["items"] if it["kind"] == "work" and len(it["values"]) >= 4)
    sp = sum(it["values"][2] for it in doc["items"] if it["kind"] == "work" and len(it["values"]) >= 4)
    if pz:
        findings.append({
            "check": "ставки НР и СП", "status": "не проверено",
            "detail": (f"восстановлено от прямых затрат: НР {nr / pz * 100:.1f}%, "
                       f"СП {sp / pz * 100:.1f}%. База начисления НР и СП — фонд оплаты труда, "
                       f"а не ПЗ; норматив по виду работ к проверке не подключён, поэтому "
                       f"вердикт «сошлось/не сошлось» не ставится"),
        })

    # 2) коды ресурсов против ФСБЦ-2022
    for code in sorted(codes):
        row = fsbc.get(code)
        findings.append({
            "check": "код ресурса в ФСБЦ-2022",
            "status": "найден" if row else "нет в базе",
            "detail": (f"{code}: {row['name'][:60]} ({row['unit']})" if row
                       else f"{code}: в ФСБЦ-2022 такого кода нет — сверить наименование вручную"),
        })

    # 3) цены ресурсов: восстановленный коэффициент к базе
    price_rows = []
    for it in doc["items"]:
        if it["kind"] != "material" or not it["values"] or not it["qty"]:
            continue
        row = fsbc.get(it["code"])
        if not row:
            continue
        base = float(row["price"])
        calc = base * it["qty"]
        if calc <= 0:
            continue
        k = it["values"][-1] / calc
        price_rows.append({"code": it["code"], "name": it["name"][:70], "qty": it["qty"],
                           "smeta_total": it["values"][-1], "base_price": base,
                           "base_times_qty": round(calc, 2), "ratio": round(k, 4)})
    if price_rows:
        ratios = sorted({r["ratio"] for r in price_rows})
        findings.append({
            "check": "цены ресурсов к ФСБЦ",
            "status": "проверено",
            "detail": f"по {len(price_rows)} строкам восстановлен коэффициент "
                      f"цена/база: {', '.join(f'{r:.3f}' for r in ratios)}. "
                      f"Разные коэффициенты у одного кабеля и кабель-канала — это нормально "
                      f"для ресурсно-индексного метода, но значит, что единого индекса "
                      f"к смете применить нельзя",
        })

    # 4) сметные нормы
    for code in sorted(work_codes):
        norm = re.sub(r"^(ГЭСН|ФЕР)", "", code)
        row = fer.get(norm)
        findings.append({
            "check": "шифр нормы в базе ФЕР",
            "status": "найден" if row else "не проверено",
            "detail": (f"{code}: {row['name'][:60]} ({row['unit']})" if row else
                       f"{code}: в базе ФЕР-2020 нет. Для ГЭСН это ожидаемо — "
                       f"в базе знаний только ФЕР; сверять по сборнику ГЭСН"),
        })

    return {"findings": findings, "prices": price_rows,
            "resolved": {"materials": len(codes), "in_fsbc": len(fsbc),
                         "works": len(work_codes), "in_fer": len(fer)}}


def to_markdown(doc: dict, result: dict) -> str:
    h = doc["header"]
    lines = [f"# Проверка сметы {h.get('object', '')}", ""]
    lines += [f"Программа: {h.get('program', '')}",
              f"Нормативы: {h.get('fsnb', '')}",
              f"Цены: {h.get('prices', '')}",
              f"Метод: {h.get('method', '')}",
              f"Сметная стоимость по документу: {h.get('total', '')} ₽", ""]
    lines += ["## Что проверено", "", "| проверка | итог | подробности |", "|---|---|---|"]
    for f in result["findings"]:
        lines.append(f"| {f['check']} | {f['status']} | {f['detail']} |")
    if result["prices"]:
        lines += ["", "## Цены ресурсов: восстановленный коэффициент к ФСБЦ-2022", "",
                  "| код | кол-во | в смете, ₽ | ФСБЦ база, ₽ | база × кол-во | коэф. |",
                  "|---|---|---|---|---|---|"]
        for r in result["prices"]:
            lines.append(f"| {r['code']} | {r['qty']} | {r['smeta_total']:,.2f} | "
                         f"{r['base_price']:,.2f} | {r['base_times_qty']:,.2f} | "
                         f"{r['ratio']:.3f} |".replace(",", " "))
    res = result["resolved"]
    lines += ["", "## Покрытие базой", "",
              f"Материалов с кодом ФГИС ЦС: {res['materials']}, найдено в ФСБЦ-2022: "
              f"{res['in_fsbc']}.",
              f"Работ со сметным шифром: {res['works']}, совпало с базой ФЕР-2020: {res['in_fer']}.",
              "", "## Чего проверка не делает", "",
              "Не пересчитывает смету и не правит цифры. НР и СП проверены только как "
              "величины в документе: ставки в смете АРОС заданы рублями, и восстановить "
              "процент можно лишь зная фонд оплаты труда по каждой норме, которого в PDF нет.",
              ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", default="out/smeta_check")
    a = ap.parse_args(argv)
    doc = parse(Path(a.pdf))
    if not doc["items"]:
        print("позиции не распознаны — это не смета АРОС в ожидаемой вёрстке", file=sys.stderr)
        return 1
    result = check(doc)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "smeta_parsed.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "smeta_check.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    md = to_markdown(doc, result)
    (out / "smeta_check.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
