# -*- coding: utf-8 -*-
"""CAD-модуль ИИ-агента ПТО: headless-работа с DXF через ezdxf (MIT).

Принципы:
  * агент не меняет чертёж напрямую — только формирует типизированные операции
    из белого списка и применяет их после подтверждения человеком;
  * каждая операция имеет источник (ссылка на КС-2/журнал/расхождение) и причину;
  * любое изменение пишется новой версией файла + записью в журнале с хэш-цепочкой.
"""
from .drawing import Drawing, open_drawing, DwgNotSupported
from .template import CorpTemplate, load_template, validate_drawing
from .ops import OPERATIONS, validate_operations, apply_operations, OperationError
from .diff import diff_drawings
from .redline import plan_from_discrepancies
from .takeoff import takeoff, compare_takeoff

__all__ = [
    "Drawing", "open_drawing", "DwgNotSupported",
    "CorpTemplate", "load_template", "validate_drawing",
    "OPERATIONS", "validate_operations", "apply_operations", "OperationError",
    "diff_drawings", "plan_from_discrepancies",
    "takeoff", "compare_takeoff",
]
