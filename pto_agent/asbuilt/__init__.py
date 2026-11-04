# -*- coding: utf-8 -*-
"""Исполнительная документация и исполнительные схемы: генерация, а не только проверка.

Пакет отвечает за три артефакта, которые инженер ПТО сегодня собирает руками:

  * **исполнительная схема** (`scheme.py`) — лист DXF + титул, штамп и таблица объёмов
    в DOCX/PNG, с ссылкой на акт КС-2 и на подтверждённые пометки;
  * **АОСР** (`aosr.py`) — акт освидетельствования скрытых работ по форме РД-11-02-2006
    (три раздела + подписи), с перечнем приложений из реестра ИД;
  * **реестр ИД** (`registry.py`) — состав комплекта с нумерацией, статусами и проверкой
    комплектности по видам работ.

Принцип тот же, что и в проверке: ничего не выдумывать. Поле, которого нет в данных,
печатается как «—» с пометкой в отчёте, а не заполняется правдоподобным значением.
"""
from .aosr import build_aosr, aosr_to_markdown               # noqa: F401
from .forms import (FORMS, CURRENT_FORM, form_status,        # noqa: F401
                    forms_catalog, get_form)
from .registry import (build_registry, check_completeness,    # noqa: F401
                       registry_to_markdown)
from .requirements import (classify_work, load_matrix,        # noqa: F401
                           matrix_summary, required_kinds,
                           work_type_requirements)
from .scheme import build_scheme, scheme_to_markdown          # noqa: F401
from .spec_from_estimate import (spec_from_estimate,          # noqa: F401
                                 spec_to_markdown)
from .validation import validate_draft                        # noqa: F401

__all__ = ["build_scheme", "scheme_to_markdown", "build_aosr", "aosr_to_markdown",
           "build_registry", "check_completeness", "registry_to_markdown",
           "validate_draft", "work_type_requirements", "required_kinds",
           "classify_work", "load_matrix", "matrix_summary", "forms_catalog",
           "form_status", "get_form", "FORMS", "CURRENT_FORM",
           "spec_from_estimate", "spec_to_markdown"]
