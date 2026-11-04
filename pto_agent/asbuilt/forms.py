# -*- coding: utf-8 -*-
"""Формы актов: что действует сейчас и что уже нельзя применять.

С 01.09.2023 состав и порядок ведения исполнительной документации определяет приказ
Минстроя России от 16.05.2023 № 344/пр (приказ действует до 01.09.2029; изменения от
23.06.2025 форму акта не затронули). Форма АОСР — приложение № 3 к нему. Прежние формы
по РД-11-02-2006 (приказ Ростехнадзора № 1128) с этой даты не применяются.

Мы держим обе формы, потому что старые объекты и архивы никто не отменял, но по умолчанию
выпускается действующая, а выпуск устаревшей формы сопровождается явным предупреждением.
"""
from __future__ import annotations

from datetime import date

FORMS = {
    "344pr": {
        "id": "344pr",
        "short": "АОСР (приложение 3 к приказу Минстроя № 344/пр)",
        "title": "АКТ освидетельствования скрытых работ",
        "legal_basis": "приказ Минстроя России от 16.05.2023 № 344/пр, приложение № 3",
        "applied_from": "2023-09-01",
        "applied_to": "2029-09-01",
        "status": "current",
        "layout": "items",
        "items": [
            {"no": 1, "key": "work", "label": "К освидетельствованию предъявлены следующие работы"},
            {"no": 2, "key": "project_docs", "label": "Работы выполнены по проектной документации"},
            {"no": 3, "key": "materials", "label": "При выполнении работ применены"},
            {"no": 4, "key": "attachments", "label": "Предъявлены документы, подтверждающие "
                                                     "соответствие работ предъявляемым к ним требованиям"},
            {"no": 5, "key": "norms", "label": "Работы выполнены в соответствии с"},
            {"no": 6, "key": "period", "label": "Сроки выполнения работ"},
            {"no": 7, "key": "next_works", "label": "Разрешается производство последующих работ"},
        ],
        "roles": [
            {"key": "customer", "label": "Представитель застройщика, технического заказчика, лица, "
                                         "ответственного за эксплуатацию здания, сооружения, или "
                                         "регионального оператора по вопросам строительного контроля"},
            {"key": "builder", "label": "Представитель лица, осуществляющего строительство, "
                                        "по вопросам строительного контроля"},
            {"key": "contractor", "label": "Представитель лица, выполнившего работы, подлежащие "
                                           "освидетельствованию"},
            {"key": "designer", "label": "Представитель лица, осуществляющего подготовку проектной "
                                         "документации (при осуществлении авторского надзора)"},
        ],
    },
    "rd-11-02-2006": {
        "id": "rd-11-02-2006",
        "short": "АОСР по РД-11-02-2006 (не применяется с 01.09.2023)",
        "title": "АКТ освидетельствования скрытых работ",
        "legal_basis": "РД-11-02-2006, приложение 3 (приказ Ростехнадзора № 1128)",
        "applied_from": "2007-01-01",
        "applied_to": "2023-09-01",
        "status": "legacy",
        "layout": "sections",
        "items": [],
        "sections": [
            "1. Представители сторон",
            "2. Исполнительная документация, предъявленная при освидетельствовании",
            "3. Работы, материалы и нормативные документы",
        ],
        "roles": [
            {"key": "customer", "label": "Застройщик (технический заказчик)"},
            {"key": "builder", "label": "Лицо, выполнившее работы"},
            {"key": "designer", "label": "Лицо, подготовившее проектную документацию"},
            {"key": "contractor", "label": "Лицо, осуществляющее строительство"},
        ],
    },
}

CURRENT_FORM = "344pr"
DEFAULT_FORM = CURRENT_FORM


def get_form(form_id: str | None = None) -> dict:
    form_id = form_id or CURRENT_FORM
    if form_id not in FORMS:
        raise ValueError(f"неизвестная форма АОСР: {form_id}; доступны: "
                         f"{', '.join(sorted(FORMS))}")
    return FORMS[form_id]


def form_status(form_id: str | None = None, as_of: date | None = None) -> dict:
    """Действует ли форма на дату: приказ № 344/пр сменил РД-11-02-2006 с 01.09.2023."""
    form = get_form(form_id)
    as_of = as_of or date.today()
    applied_from = date.fromisoformat(form["applied_from"])
    applied_to = date.fromisoformat(form["applied_to"])
    active = applied_from <= as_of < applied_to
    return {
        "form": form["id"],
        "short": form["short"],
        "legal_basis": form["legal_basis"],
        "status": form["status"],
        "applied_from": form["applied_from"],
        "applied_to": form["applied_to"],
        "active_on_date": active,
        "current_form": CURRENT_FORM,
        "warning": None if active else (
            f"форма {form['id']} не применяется с {form['applied_from'] if form['status']=='current' else form['applied_to']}; "
            f"действующая — {CURRENT_FORM}"),
    }


def forms_catalog() -> dict:
    return {"current": CURRENT_FORM,
            "forms": [{k: v[k] for k in ("id", "short", "legal_basis", "applied_from",
                                         "applied_to", "status")} for v in FORMS.values()]}
