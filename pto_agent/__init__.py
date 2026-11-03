# -*- coding: utf-8 -*-
"""ИИ-агент ПТО — MVP (v0.1).

Установка зависимостей:  pip install -r requirements-pto.txt
Полный цикл:             python -m pto_agent run --data data/demo --out out
Нормативный запрос:      python -m pto_agent norms "толщина защитного слоя бетона"
Проверка журнала:        python -m pto_agent audit-verify --path out/audit.jsonl
Маршрутизация модели:    python -m pto_agent route --intent ks2_validate --data-class commercial_secret
"""

__version__ = "0.1.0"
