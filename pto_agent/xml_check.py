# -*- coding: utf-8 -*-
"""Проверка XML перед сдачей: структура по профилю контура и внешняя XSD, если она есть.

Честная рамка, без которой модуль вреден:

* XSD-схем ФНС в контуре **нет** — `format.nalog.ru` недоступен, а перепечатать схему по
  памяти значит получить валидатор, который подтверждает неправильные файлы. Поэтому
  проверка по профилю (наш файл `data/reference/fns_xml_profile.json`) отвечает только на
  вопрос «файл устроен так, как мы сами объявили обязательным», и никогда не значит
  «пройдёт приём».
* Если человек передал настоящую XSD (скачал у ФНС и положил в контур), модуль валидирует
  по ней через lxml и показывает ошибки схемы с номерами строк. Это и есть A4: механизм
  готов, схема — вопрос доступа.
* Проверки, которые не зависят от схемы, работают всегда: ИНН по контрольной сумме, КПП по
  структуре, формат дат, формат денежных значений, обязательные реквизиты строки.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from . import tax

SCHEMA = "pto-agent/xml-profile@1"
PROFILE_PATH = (Path(__file__).resolve().parents[1] / "data" / "reference"
                / "fns_xml_profile.json")

MONEY = re.compile(r"^-?\d+(\.\d{1,2})?$")
DATE_DMY = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
DATE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_profile(path: str | Path | None = None) -> dict:
    return json.loads(Path(path or PROFILE_PATH).read_text(encoding="utf-8"))


def _local(tag: str) -> str:
    """Имя элемента без пространства имён: наши отчёты живут в своём namespace."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _children(element) -> list:
    return list(element)


def _path(element, path: str) -> list:
    """Все узлы по пути из локальных имён (namespace игнорируется намеренно)."""
    nodes = [element]
    for part in path.split("/"):
        nxt = []
        for node in nodes:
            nxt.extend(child for child in node if _local(child.tag) == part)
        nodes = nxt
        if not nodes:
            return []
    return nodes


class Report(dict):
    """Отчёт проверки: ошибки, предупреждения, что именно проверялось."""

    def add(self, code: str, where: str, why: str) -> None:
        self["errors"].append({"code": code, "where": where, "why": why})

    def warn(self, code: str, where: str, why: str) -> None:
        self["warnings"].append({"code": code, "where": where, "why": why})


def validate(xml_text: str, *, profile_kind: str = "books",
             xsd_path: str | Path | None = None,
             profile: dict | None = None) -> dict:
    """Проверить XML. Возвращает отчёт: `ok`, `errors`, `warnings`, `checks`, `schema`."""
    profile = profile or load_profile()
    specs = profile.get("profiles") or {}
    if profile_kind not in specs:
        raise ValueError(f"профиль {profile_kind!r} не заведён; есть: {', '.join(specs)}")
    spec = specs[profile_kind]
    report = Report(ok=False, profile=profile_kind, profile_title=spec.get("title"),
                    errors=[], warnings=[], checks=[], xsd=None,
                    schema_source="профиль контура (не XSD ФНС)",
                    note=profile.get("note") or "")

    # 1. well-formed
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        report.add("XML_NOT_WELL_FORMED", "документ", f"XML не разбирается: {error}")
        return report
    report["checks"].append("XML разбирается как дерево")

    # 2. корень и его атрибуты
    expected_root = str(spec.get("root") or "")
    if _local(root.tag) != expected_root:
        report.add("XML_ROOT", _local(root.tag),
                   f"корневой элемент «{_local(root.tag)}», ожидался «{expected_root}»")
    for name in spec.get("root_attrs_required") or []:
        if not str(root.attrib.get(name) or "").strip():
            report.add("XML_ROOT_ATTR", f"{expected_root}@{name}",
                       f"у корня нет обязательного атрибута «{name}»")
    for name, allowed in (spec.get("root_attrs_values") or {}).items():
        value = root.attrib.get(name)
        if value is not None and str(value) not in [str(item) for item in allowed]:
            report.add("XML_ROOT_ATTR_VALUE", f"{expected_root}@{name}",
                       f"значение «{value}» не из списка {allowed}")
    report["checks"].append("корневой элемент и обязательные атрибуты на месте")

    # 3. обязательные блоки
    for path in spec.get("required_paths") or []:
        if not _path(root, path):
            report.add("XML_MISSING_BLOCK", path, f"нет обязательного блока «{path}»")
    report["checks"].append("обязательные блоки присутствуют")

    # 4. строки: обязательные элементы и атрибуты
    row_nodes = []
    for path in spec.get("row_paths") or []:
        row_nodes.extend(_path(root, path))
    seen_required = {name: False for name in spec.get("row_required_children") or []}
    for index, row in enumerate(row_nodes, 1):
        where = f"{_local(row.tag)} № {index}"
        names = {_local(child.tag) for child in row}
        for name in spec.get("row_required_children") or []:
            if name in names:
                seen_required[name] = True
            else:
                report.add("XML_ROW_FIELD", f"{where}/{name}",
                           f"в строке нет обязательного элемента «{name}»")
        for element_name, attrs in (spec.get("row_required_attrs") or {}).items():
            targets = [row] if element_name == "row" else [
                child for child in row if _local(child.tag) == element_name]
            for target in targets:
                for attr in attrs:
                    if not str(target.attrib.get(attr) or "").strip():
                        report.add("XML_ROW_ATTR", f"{where}/{_local(target.tag)}@{attr}",
                                   f"нет обязательного атрибута «{attr}»")
        code_spec = (spec.get("code_elements") or {}).get("КодВидОпер")
        if code_spec:
            codes = [child for child in row if _local(child.tag) == "КодВидОпер"]
            for code in codes:
                value = str(code.text or "").strip()
                if code_spec.get("digits") and not value.isdigit():
                    report.add("XML_CODE", where,
                               f"код вида операции «{value}» не число")
                if not (int(code_spec.get("min") or 0) <= len(value)
                        <= int(code_spec.get("max") or 99)):
                    report.add("XML_CODE", where,
                               f"код вида операции «{value}» длины {len(value)}, "
                               f"ожидалась {code_spec.get('min')}–{code_spec.get('max')}")
    if row_nodes:
        report["checks"].append(f"строк проверено: {len(row_nodes)}")

    # 5. реквизиты: ИНН и КПП
    for name in spec.get("inn_elements") or []:
        # ИНН бывает и элементом (ИНН), и атрибутом (ИННЮЛ/ИННФЛ в формате книг) —
        # ищем оба варианта, иначе проверка контрольной суммы молча не выполняется
        nodes = [node for node in root.iter() if _local(node.tag) == name]
        nodes += [node for node in root.iter() if str(node.attrib.get(name) or "").strip()]
        for node in nodes:
            # ИНН может быть и элементом, и атрибутом (ИНН/ИННЮЛ/ИННФЛ) — смотрим оба места
            value = str(node.text or "").strip()
            if not value:
                for attr in ("ИНН", "ИННЮЛ", "ИННФЛ"):
                    value = str(node.attrib.get(attr) or "").strip()
                    if value:
                        break
            if not value:
                report.add("XML_INN_EMPTY", name, "ИНН пуст")
            elif not tax.inn_ok(value):
                report.add("XML_BAD_INN", f"{name}={value}",
                           "ИНН не проходит проверку контрольной суммы")
    for name in spec.get("kpp_attrs") or []:
        for node in root.iter():
            value = str(node.attrib.get(name) or "").strip()
            if value and not tax.kpp_ok(value):
                report.add("XML_BAD_KPP", f"{_local(node.tag)}@{name}={value}",
                           "КПП не той структуры: 4 цифры + буква + 5 цифр")
    report["checks"].append("ИНН и КПП проверены по контрольным суммам")

    # 6. даты
    formats = spec.get("date_formats") or []
    for path in spec.get("date_values") or []:
        # путь может указывать на атрибут: «Документ@ДатаДок» — тогда дату берём из атрибута
        if "@" in path:
            element_path, attr = path.split("@", 1)
            nodes = _path(root, element_path)
            values = [str(node.attrib.get(attr) or "").strip() for node in nodes]
        else:
            nodes = _path(root, path)
            values = [str(node.text or "").strip() for node in nodes]
        for value in values:
            if not value:
                continue
            if not any(regex.match(value) for regex in
                       (DATE_DMY if "DD.MM.YYYY" in formats else DATE_ISO,
                        DATE_ISO if "DD.MM.YYYY" in formats else DATE_DMY)):
                report.add("XML_DATE", f"{path}={value}", "дата не в допустимом формате")
            else:
                try:
                    if DATE_DMY.match(value):
                        day, month, year = value.split(".")
                        _ = (int(day), int(month), int(year))
                        if not (1 <= int(day) <= 31 and 1 <= int(month) <= 12):
                            raise ValueError
                    else:
                        from datetime import date as _date
                        _date.fromisoformat(value)
                except ValueError:
                    report.add("XML_DATE", f"{path}={value}", "дата не существует")
    report["checks"].append("даты проверены")

    # 7. деньги
    allow_negative = set(spec.get("money_allow_negative") or [])
    for name in spec.get("money_elements") or []:
        for node in root.iter():
            if _local(node.tag) != name:
                continue
            value = str(node.text or "").strip()
            if not MONEY.match(value):
                report.add("XML_MONEY", f"{name}={value}",
                           "сумма не в формате «0.00» (точка, не более двух знаков)")
            elif value.startswith("-") and name not in allow_negative:
                report.add("XML_MONEY_NEGATIVE", f"{name}={value}",
                           "отрицательная сумма там, где она не предусмотрена")
    report["checks"].append("денежные значения проверены")

    # 8. самопроверка: файл заявляет, что со схемой не сверялся
    if str(root.attrib.get("СвереноСоСхемой") or "").lower() == "нет":
        report.warn("XML_NOT_SCHEMA_CHECKED", "Файл@СвереноСоСхемой",
                    "файл сам заявляет, что со схемой XSD не сверялся: приём в ФНС не "
                    "гарантирован, это черновик")

    # 9. внешняя XSD, если человек её передал
    if xsd_path:
        report["xsd"] = _validate_xsd(xml_text, xsd_path, report)
    else:
        report.warn("XML_NO_XSD", "--xsd",
                    "схема XSD не передана: проверена только структура по профилю контура; "
                    "XSD ФНС в контуре нет (format.nalog.ru недоступен)")

    report["ok"] = not report["errors"]
    return report


def _validate_xsd(xml_text: str, xsd_path, report: Report) -> dict:
    """Проверка по настоящей XSD. Без lxml — честный отказ, а не «всё хорошо»."""
    path = Path(xsd_path)
    if not path.exists():
        report.add("XSD_NOT_FOUND", str(path), "файл схемы не найден")
        return {"path": str(path), "ok": False}
    try:
        from lxml import etree
    except ImportError:  # pragma: no cover — в CI lxml есть
        report.warn("XSD_NO_LXML", str(path),
                    "lxml не установлен: проверка по XSD не выполнена, это не «успех»")
        return {"path": str(path), "ok": False, "skipped": "нет lxml"}
    try:
        schema = etree.XMLSchema(etree.parse(str(path)))
    except Exception as error:  # noqa: BLE001 — битая схема должна быть видна, а не падать
        report.add("XSD_BROKEN", str(path), f"схема не читается: {error}")
        return {"path": str(path), "ok": False}
    document = etree.fromstring(xml_text.encode("utf-8"))
    valid = schema.validate(document)
    errors = [{"line": item.line, "message": item.message}
              for item in schema.error_log]
    if not valid:
        for item in errors[:20]:
            report.add("XSD_INVALID", f"строка {item['line']}", item["message"])
    report["schema_source"] = f"XSD: {path}"
    report["checks"].append("файл проверен по переданной XSD")
    return {"path": str(path), "ok": valid, "errors": errors[:20]}


def to_markdown(report: dict) -> str:
    lines = ["# Проверка XML", ""]
    lines.append(f"Профиль: **{report.get('profile')}** — {report.get('profile_title')}")
    lines.append(f"Источник схемы: {report.get('schema_source')}")
    if report.get("note"):
        lines.append("")
        lines.append(f"> {report['note']}")
    lines.append("")
    lines.append("## Что проверено")
    for text in report.get("checks") or []:
        lines.append(f"- {text}")
    if report.get("errors"):
        lines.append("")
        lines.append("## Ошибки")
        for item in report["errors"]:
            lines.append(f"- **{item['code']}** · {item['where']}: {item['why']}")
    else:
        lines.append("")
        lines.append("## Ошибки")
        lines.append("- нет: структура по профилю сходится")
    if report.get("warnings"):
        lines.append("")
        lines.append("## Предупреждения")
        for item in report["warnings"]:
            lines.append(f"- {item['code']} · {item['where']}: {item['why']}")
    lines.append("")
    lines.append("## Не сделано")
    lines.append("- проверка по профилю не заменяет приём в ФНС: XSD-схемы у контура нет, "
                 "и «ошибок нет» здесь означает «по нашему профилю сходится»")
    lines.append("- подпись УКЭП не проверяется и не ставится")
    return "\n".join(lines)
