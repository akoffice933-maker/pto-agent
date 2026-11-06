# -*- coding: utf-8 -*-
"""Разбор чисел из внешних документов — одно правило на весь проект.

Зачем отдельный модуль: `float()` в Python принимает `nan`, `inf` и `-inf`. Строка «nan» в
выгрузке обмера или в ведомости расхода стали проходила как «число», дальше `nan` попадал в
объём, в сумму и в итог — а сумма с `nan` равна `nan`, и все сравнения с ним ложны
(`abs(nan - x) > допуск` → `False`), то есть **проверка арифметики молча пропускала ошибку**.

Здесь же собраны варианты записи, которые встречаются в сметах и ведомостях:
`1 234,56`, `1 234.56`, неразрывный пробел, юникодный минус `−12`, запись в скобках
`(1 234,56)` как отрицательное, `12,5 м3` (единица отбрасывается неявно — только цифры в
начале строки не берём: см. `strict`).
"""
from __future__ import annotations

import math


def parse_number(value, *, allow_negative: bool = True) -> float | None:
    """Строка/число → конечное число или `None`.

    `None` означает «не число» и не должен превращаться в ноль: ноль — это тоже данные.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace("\u00a0", "").replace("\u2009", "").replace(" ", "")
        text = text.replace("\u2212", "-").replace("−", "-").replace("₽", "")
        negative_brackets = text.startswith("(") and text.endswith(")")
        if negative_brackets:
            text = "-" + text[1:-1]
        text = text.replace(",", ".")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if not math.isfinite(number):
        return None
    if not allow_negative and number < 0:
        return None
    return number


def is_number(value) -> bool:
    """`True`, если значение разбирается как конечное число."""
    return parse_number(value) is not None


def positive(value) -> float | None:
    """Разбирается как конечное число и строго больше нуля — иначе `None`.

    Нужно для множителей: `0` в габарите или толщине — это не «ноль кубометров», а
    незаполненное или испорченное значение.
    """
    number = parse_number(value)
    return number if number is not None and number > 0 else None
