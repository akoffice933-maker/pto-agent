# -*- coding: utf-8 -*-
"""Сквозная проверка валидатора XML: профиль контура и внешняя XSD.

Вопросы, на которые отвечает скрипт:

1. файл, который выпускает контур, проходит по обязательным блокам и реквизитам;
2. ошибки ловятся: нет кода вида операции, битый КПП, ИНН без верной контрольной суммы,
   сумма с запятой, отрицательная сумма, несуществующая дата, неразбираемый XML;
3. битая или неподходящая XSD — это ошибка, а не «всё хорошо»; отсутствие схемы —
   предупреждение, а не успех;
4. «ошибок нет» никогда не подаётся как «файл пройдёт приём в ФНС».

Запуск: `python3 scripts/check_xml.py`. Код возврата 1, если есть нарушения.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pto_agent import invoices, ledger, tax, xml_check  # noqa: E402

checks = {"done": 0}
violations: list[str] = []


def check(condition: bool, message: str) -> None:
    checks["done"] += 1
    if not condition:
        violations.append(message)


GOOD = """<?xml version="1.0" encoding="UTF-8"?>
<Файл ИдФайл="KND_1115102_7810447576781001001_202610_1" ВерсФорм="5.06" ВерсПрог="ИИ-агент ПТО"
      ТипИнф="КНД 1115102" СвереноСоСхемой="нет">
  <Документ КНД="1115102" ДатаДок="2026-10-01" Период="24" ОтчетГод="2026">
    <СвПокуп НаимОрг="ООО «СтройМонтаж»" ИННЮЛ="7810447576" КПП="781001001"/>
    <СвПокупка НомПП="1">
      <КодВидОпер>01</КодВидОпер>
      <НомДатаСчФ НомерСчФ="СФ №341" ДатаСчФ="01.10.2026"/>
      <СтТовУчНалВсего>1739720.00</СтТовУчНалВсего>
      <ВтчСумНДС>313720.00</ВтчСумНДС>
    </СвПокупка>
  </Документ>
</Файл>"""

# ── 1. профиль ──────────────────────────────────────────────────────────────
profile = xml_check.load_profile()
check(profile.get("schema") == xml_check.SCHEMA,
      f"схема профиля {profile.get('schema')} вместо {xml_check.SCHEMA}")
check(set(("books", "vat")) <= set(profile.get("profiles") or {}),
      "в профиле нет books или vat")
check("НЕ схема ФНС" in str(profile.get("note")), "профиль не предупреждает, что это не XSD ФНС")
check(bool(profile.get("not_implemented")), "список «не сделано» профиля пуст")

# ── 2. положительный случай ─────────────────────────────────────────────────
report = xml_check.validate(GOOD, profile_kind="books")
check(report["ok"] is True, f"годный файл не прошёл: {report['errors']}")
check(report["schema_source"].startswith("профиль контура"),
      "источник схемы не назван профилем контура")
codes = [item["code"] for item in report["warnings"]]
check("XML_NO_XSD" in codes, "нет предупреждения, что XSD не передана")
check("XML_NOT_SCHEMA_CHECKED" in codes, "нет предупреждения, что файл не сверен со схемой")
check("не заменяет" in xml_check.to_markdown(report),
      "в отчёте не сказано, что профиль не заменяет приём в ФНС")

# ── 3. ошибки ───────────────────────────────────────────────────────────────
cases = {
    "XML_ROW_FIELD": GOOD.replace("      <КодВидОпер>01</КодВидОпер>\n", ""),
    "XML_BAD_KPP": GOOD.replace('КПП="781001001"', 'КПП="78-1001001"'),
    "XML_BAD_INN": GOOD.replace('ИННЮЛ="7810447576"', 'ИННЮЛ="1234567890"'),
    "XML_MONEY": GOOD.replace("313720.00", "313720,00"),
    "XML_MONEY_NEGATIVE": GOOD.replace("313720.00", "-313720.00"),
    "XML_DATE": GOOD.replace('ДатаСчФ="01.10.2026"', 'ДатаСчФ="32.10.2026"'),
    "XML_ROOT_ATTR": GOOD.replace('ТипИнф="КНД 1115102" ', ""),
    "XML_ROOT_ATTR_VALUE": GOOD.replace('СвереноСоСхемой="нет"', 'СвереноСоСхемой="не знаю"'),
}
for code, text in cases.items():
    found = [item["code"] for item in xml_check.validate(text)["errors"]]
    check(code in found, f"ошибка {code} не поймана (найдено: {found})")
check(xml_check.validate("<Файл><Документ>")["errors"][0]["code"] == "XML_NOT_WELL_FORMED",
      "неразбираемый XML не назван")

# ── 4. файл контура ─────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    from pto_agent.web import build_demo

    build_demo(folder)
    state = ledger.load(folder / "ledger" / "state.json")
    acts = json.loads((ROOT / "data/demo/accounting/acts_signed_scenario.json").read_text("utf-8"))
    ledger.post_acts(state, acts, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    reg = invoices.new_state()
    incoming = json.loads((ROOT / "data/demo/purchases/incoming_2026_10.json").read_text("utf-8"))
    invoices.auto_register_purchases(reg, incoming, "2026-10", ledger_state=state)
    books_xml = invoices.to_fns_xml(invoices.purchase_book(reg, state, "2026-10"))
    report = xml_check.validate(books_xml, profile_kind="books")
    check(report["ok"] is True, f"книга покупок контура не прошла профиль: {report['errors']}")

    vat_xml = tax.to_xml(tax.vat_return(state, "2026-10"))
    vat_report = xml_check.validate(vat_xml, profile_kind="vat")
    check(vat_report["ok"] is True, f"отчёт по НДС не прошёл профиль: {vat_report['errors']}")

    # ── 5. XSD ──────────────────────────────────────────────────────────────
    schema = folder / "mini.xsd"
    schema.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="Файл"><xs:complexType><xs:sequence>'
        '<xs:element name="Документ" minOccurs="1"><xs:complexType><xs:sequence>'
        '<xs:any processContents="skip" minOccurs="0" maxOccurs="unbounded"/>'
        '</xs:sequence><xs:anyAttribute processContents="skip"/></xs:complexType></xs:element>'
        '</xs:sequence><xs:anyAttribute processContents="skip"/></xs:complexType></xs:element>'
        '</xs:schema>', encoding="utf-8")
    with_xsd = xml_check.validate(GOOD, xsd_path=schema)
    check(with_xsd["xsd"]["ok"] is True, f"файл не прошёл по переданной XSD: {with_xsd['errors']}")
    check(with_xsd["schema_source"].startswith("XSD:"), "источник схемы не назван XSD")

    wrong = folder / "wrong.xsd"
    wrong.write_text('<?xml version="1.0"?><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
                     '<xs:element name="ДругойКорень" type="xs:string"/></xs:schema>',
                     encoding="utf-8")
    bad = xml_check.validate(GOOD, xsd_path=wrong)
    check("XSD_INVALID" in [item["code"] for item in bad["errors"]],
          "файл, не подходящий схеме, признан годным")
    check(bad["ok"] is False, "отчёт с ошибками схемы назван успешным")

    broken = folder / "broken.xsd"
    broken.write_text("<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>", encoding="utf-8")
    hurt = xml_check.validate(GOOD, xsd_path=broken)
    check("XSD_BROKEN" in [item["code"] for item in hurt["errors"]], "битая схема не названа")
    missing = xml_check.validate(GOOD, xsd_path=folder / "нет.xsd")
    check("XSD_NOT_FOUND" in [item["code"] for item in missing["errors"]],
          "отсутствующий файл схемы не назван")

try:
    xml_check.validate(GOOD, profile_kind="нет-такого")
    check(False, "неизвестный профиль не отклонён")
except ValueError:
    check(True, "")

print(f"проверок: {checks['done']}, нарушений: {len(violations)}")
for item in violations:
    print(f"  ✗ {item}")
sys.exit(1 if violations else 0)
