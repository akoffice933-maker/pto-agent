# -*- coding: utf-8 -*-
"""Распознавание сканов redline: локальная VL-модель (или офлайн-CV) → операции.

Публичный контракт пакета:

    analyze_scan(scan, drawing=..., engine="vlm" | "offline" | "mock")
        → dict с marks / rejected / stats / warnings / calibration
    plan_from_scan(run, drawing, template, summary)
        → dict с operations / conflicts / needs_human_decision

Ключевой принцип: ответ модели — не истина. Каждый bbox проверяется по картинке
(есть ли под ним чернила), калибровка скана не принимается без подтверждения
человека, а всё, что ниже порога уверенности или конфликтует с КС-2/журналом,
уходит в вопросы человеку, а не в операции.
"""
from .calibrate import Calibration, CalibrationError
from .engine import analyze_scan
from .plan import CONFLICT_APPROVAL, CONFLICT_QUANTITY, plan_from_scan
from .providers import ProviderUnavailable

__all__ = [
    "Calibration", "CalibrationError", "analyze_scan", "plan_from_scan",
    "CONFLICT_APPROVAL", "CONFLICT_QUANTITY", "ProviderUnavailable",
]
