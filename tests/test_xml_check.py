# -*- coding: utf-8 -*-
"""Проверка XML: профиль контура и внешняя XSD.

Главное, что здесь проверяется, — **честность** валидатора:

1. файл, который контур выпускает, проходит по обязательным блокам и реквизитам;
2. ошибки ловятся по делу: нет кода вида операции, битый КПП, сумма с запятой, отрицательная
   сумма там, где её быть не может, несуществующая дата, неразбираемый XML;
3. битая схема XSD — это ошибка, а не «всё хорошо»; отсутствие схемы — предупреждение;
4. успешная проверка по профилю **не** объявляется приёмом в ФНС: в отчёте это написано.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pto_agent import invoices, ledger, tax, xml_check

REPO = Path(__file__).resolve().parents[1]

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


def demo_books_xml() -> str:
    from pto_agent.web import build_demo

    with pytest.MonkeyPatch.context():
        import tempfile
        folder = Path(tempfile.mkdtemp())
    build_demo(folder)
    state = ledger.load(folder / "ledger" / "state.json")
    acts = json.loads((REPO / "data/demo/accounting/acts_signed_scenario.json")
                      .read_text(encoding="utf-8"))
    ledger.post_acts(state, acts, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    reg = invoices.new_state()
    incoming = json.loads((REPO / "data/demo/purchases/incoming_2026_10.json")
                          .read_text(encoding="utf-8"))
    invoices.auto_register_purchases(reg, incoming, "2026-10", ledger_state=state)
    return invoices.to_fns_xml(invoices.purchase_book(reg, state, "2026-10"))


# ─────────────────────────── структура ───────────────────────────
def test_good_file_passes_with_honest_warnings():
    report = xml_check.validate(GOOD, profile_kind="books")
    assert report["ok"] is True
    assert report["errors"] == []
    codes = [item["code"] for item in report["warnings"]]
    assert "XML_NO_XSD" in codes
    assert "XML_NOT_SCHEMA_CHECKED" in codes
    assert "не заменяет" in xml_check.to_markdown(report)


def test_real_report_from_contour_passes():
    xml = demo_books_xml()
    report = xml_check.validate(xml, profile_kind="books")
    assert report["ok"] is True, report["errors"]


def test_missing_operation_code_is_an_error():
    broken = GOOD.replace("      <КодВидОпер>01</КодВидОпер>\n", "")
    report = xml_check.validate(broken)
    assert [e["code"] for e in report["errors"]] == ["XML_ROW_FIELD"]


def test_bad_kpp_and_inn_checksum():
    broken = GOOD.replace('КПП="781001001"', 'КПП="78-1001001"')
    report = xml_check.validate(broken)
    assert "XML_BAD_KPP" in [e["code"] for e in report["errors"]]
    broken_inn = GOOD.replace('ИННЮЛ="7810447576"', 'ИННЮЛ="1234567890"')
    assert "XML_BAD_INN" in [e["code"] for e in xml_check.validate(broken_inn)["errors"]]


def test_money_format_and_negative():
    comma = GOOD.replace("313720.00", "313720,00")
    assert "XML_MONEY" in [e["code"] for e in xml_check.validate(comma)["errors"]]
    negative = GOOD.replace("313720.00", "-313720.00")
    assert "XML_MONEY_NEGATIVE" in [e["code"] for e in xml_check.validate(negative)["errors"]]


def test_date_must_exist():
    broken = GOOD.replace('ДатаСчФ="01.10.2026"', 'ДатаСчФ="32.10.2026"')
    assert "XML_DATE" in [e["code"] for e in xml_check.validate(broken)["errors"]]


def test_not_well_formed():
    report = xml_check.validate("<Файл><Документ>")
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "XML_NOT_WELL_FORMED"


def test_root_and_attributes():
    broken = GOOD.replace('ТипИнф="КНД 1115102" ', "")
    assert "XML_ROOT_ATTR" in [e["code"] for e in xml_check.validate(broken)["errors"]]
    wrong_root = GOOD.replace("<Файл ", "<Документ ").replace("</Файл>", "</Документ>")
    assert "XML_ROOT" in [e["code"] for e in xml_check.validate(wrong_root)["errors"]]


def test_unknown_profile_is_refused():
    with pytest.raises(ValueError):
        xml_check.validate(GOOD, profile_kind="нет-такого")


def test_vat_report_profile():
    from pto_agent.web import build_demo
    import tempfile

    folder = Path(tempfile.mkdtemp())
    build_demo(folder)
    state = ledger.load(folder / "ledger" / "state.json")
    acts = json.loads((REPO / "data/demo/accounting/acts_signed_scenario.json")
                      .read_text(encoding="utf-8"))
    ledger.post_acts(state, acts, contract="Д-142/2026", customer="ООО «Заказчик-Инвест»")
    xml = tax.to_xml(tax.vat_return(state, "2026-10"))
    report = xml_check.validate(xml, profile_kind="vat")
    assert report["ok"] is True, report["errors"]


# ─────────────────────────── XSD ───────────────────────────
MINI_XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="Файл">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="Документ" minOccurs="1" maxOccurs="1">
          <xs:complexType>
            <xs:sequence>
              <xs:any processContents="skip" minOccurs="0" maxOccurs="unbounded"/>
            </xs:sequence>
            <xs:attribute name="КНД" type="xs:string" use="required"/>
            <xs:anyAttribute processContents="skip"/>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
      <xs:attribute name="ИдФайл" type="xs:string" use="required"/>
      <xs:anyAttribute processContents="skip"/>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


def test_xsd_validation_works_when_schema_is_given(tmp_path):
    schema = tmp_path / "mini.xsd"
    schema.write_text(MINI_XSD, encoding="utf-8")
    report = xml_check.validate(GOOD, xsd_path=schema)
    assert report["xsd"]["ok"] is True
    assert report["schema_source"].startswith("XSD:")


def test_file_against_wrong_xsd_reports_schema_errors(tmp_path):
    schema = tmp_path / "other.xsd"
    schema.write_text(
        '<?xml version="1.0"?><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="НеТотКорень" type="xs:string"/></xs:schema>', encoding="utf-8")
    report = xml_check.validate(GOOD, xsd_path=schema)
    assert report["ok"] is False
    assert "XSD_INVALID" in [e["code"] for e in report["errors"]]


def test_broken_schema_is_an_error_not_success(tmp_path):
    schema = tmp_path / "broken.xsd"
    schema.write_text("<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>",
                      encoding="utf-8")
    report = xml_check.validate(GOOD, xsd_path=schema)
    assert "XSD_BROKEN" in [e["code"] for e in report["errors"]]
    assert report["ok"] is False


def test_missing_schema_file(tmp_path):
    report = xml_check.validate(GOOD, xsd_path=tmp_path / "нет.xsd")
    assert "XSD_NOT_FOUND" in [e["code"] for e in report["errors"]]
