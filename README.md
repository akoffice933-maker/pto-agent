# ИИ-агент ПТО

[![CI](https://github.com/akoffice933-maker/pto-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/akoffice933-maker/pto-agent/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![MCP](https://img.shields.io/badge/MCP-2025--06--18-1F3864)
![Tests](https://img.shields.io/badge/tests-172%20passed-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

**Агент для ПТО в строительстве:** сверяет КС-2 с журналом и сметой, распознаёт выпуск
исполнительной документации со сканов и фотографий, переводит каждый спор с актом в рубли
и подключается к Claude Desktop / Cursor / VS Code по Model Context Protocol.

Главный принцип: **ни одна цифра не выдумывается**. У каждой суммы есть формула и источник,
где данных нет — «не оценено» с причиной, а не ноль; чертёж не меняется без подтверждения
человека; каждый вызов инструмента пишется в журнал с хэш-цепочкой.

| Что умеет | Результат на демо-объекте |
|---|---|
| Сверка КС-2, объёмов, комплектности ИД | 7 расхождений из 7 заложенных, 0 лишних |
| Выпуск ИД целиком (30–50 листов) | 40 листов, 205 пометок, 43 конфликта с актом |
| Фото с телефона → чертёж | гомография по 4 точкам, rms 0,0 мм, 5/5 пометок |
| Деньги по выпуску | 1 850 000 ₽ под риском = 5,42 % акта, каждый рубль с формулой |
| Портфель актов объекта | 3 акта, покрытие 100 %, 2 091 000 ₽ под риском (5,43 %) |
| MCP | 15 инструментов, stdio + http, установка одной командой |

## Быстрый старт

```bash
git clone https://github.com/akoffice933-maker/pto-agent.git && cd pto-agent
pip install -r requirements-pto.txt

# 1. демо-данные и чертежи
python3 scripts/make_demo_data.py && python3 scripts/make_demo_dxf.py
python3 scripts/make_demo_redline.py && python3 scripts/make_demo_batch.py

# 2. сверка КС-2 и объёмов
python3 -m pto_agent run --data data/demo --out out

# 3. выпуск исполнительной документации со сканов
python3 -m pto_agent cad scan-batch --manifest data/demo/batch/manifest.json \
        --engine vlm --summary out/summary.json --out out/cad/batch

# 4. деньги: сколько стоит спор с актом
python3 -m pto_agent cad impact --batch out/cad/batch/batch_summary.json \
        --summary out/summary.json --out out/cad/impact --with-background

# 5. страница руководителя
python3 docs/build_dashboard.py && open Дашборд_руководителя_ПТО.html

# 6. тесты
python3 -m pytest tests -q
```

## MCP: подключить к своему клиенту

```bash
python3 scripts/install_mcp.py --client claude --write   # Claude Desktop
python3 scripts/install_mcp.py --client cursor --write   # Cursor
python3 scripts/install_mcp.py --client vscode --write   # VS Code
python3 -m pto_agent.mcp_server --transport http --port 8765   # http + инспектор
docker build -t pto-agent-mcp:0.6 . && docker run -p 8765:8765 pto-agent-mcp:0.6
```

15 инструментов: чтение DXF и takeoff, распознавание сканов и план правок, применение
операций только с `confirm{by, role, comment}`, пакетная обработка выпуска, деньги по акту
и по портфелю актов. Подробно — [`docs/10_MCP_обвязка.md`](docs/10_MCP_обвязка.md).

## Репозиторий

**https://github.com/akoffice933-maker/pto-agent** — публичный, MIT.

```bash
git clone https://github.com/akoffice933-maker/pto-agent.git
```

Что настроено: `.gitignore` (артефакты прогонов и тяжёлые демо-сканы в git не едут —
генерируются скриптами), `LICENSE` (MIT), CI на GitHub Actions: `unit` на Python 3.11/3.12 →
`e2e` со сквозным прогоном (выпуск → деньги → дашборд) → сборка Docker-образа.
Чтобы запушить в свой форк: `./scripts/publish_github.sh git@github.com:ВАШ_АККАУНТ/pto-agent.git`.

---

# Пакет проекта: что внутри

Дата: 02.11.2026 · Версия пакета: 1.2

## 0. MVP: работающий прототип агента

Решение: проект делаем сами, готовые open-source проекты используем как подспорье (с фиксацией лицензий).
Прототип собран и прогнан на демо-наборе: **7 расхождений из 7 заложенных найдено, 17 тестов зелёные**.

```bash
pip install -r requirements-pto.txt
python3 scripts/make_demo_data.py          # демо-смета, журнал, ранее закрытые акты, нормативы
python3 -m pto_agent run --data data/demo --out out
python3 -m pto_agent approve --out out --by "Иванов И.И." --decision returned_for_revision --comment "…"
python3 -m pto_agent audit-verify --path out/audit.jsonl
python3 scripts/check_expected.py          # 7 из 7 заложенных расхождений, без лишних
python3 -m pytest tests -q
```

| Файл | Что внутри |
|---|---|
| `11_Смежные_проекты_и_лицензии.docx` (`docs/11_…md`) | **Смежные проекты:** 10 открытых сметных/обмерных проектов с проверенными звёздами и лицензиями, матрица «код можно / нельзя» (AGPL и «без лицензии» — только идеи), три сценария стыковки (внешний MCP-обмер, база ГЭСН/ФЕР, паттерны multi-agent), чего сознательно не делаем, дорожная карта |
| `10_MCP_обвязка.docx` (`docs/10_…md`) | **MCP одной командой:** установщик конфигов для Claude Desktop / Cursor / VS Code, HTTP-транспорт и Docker, настройки из окружения, 15 тестов протокола, правила безопасности |
| `09_Портфель_актов_и_график_финансирования.docx` (`docs/09_…md`) | **Деньги под риском по объекту:** реестр актов, покрытие проверкой 100 % после ретро-выпуска, разбивка по месяцам; непроверенный акт — «не оценено», а не «0 ₽» |
| `08_Деньги_по_выпуску.docx` (`docs/08_…md`) | **Сколько стоит спор:** конфликты пометок и расхождения ПТО → рубли по ценам сметы/КС-2; 1 850 000 ₽ под риском (5,42 % акта) на демо-выпуске, каждый рубль с формулой, «не оценено» вместо нуля |
| `07_Фото_с_телефона_и_пакетная_обработка.docx` (`docs/07_…md`) | **Фото с телефона и выпуск целиком:** калибровка по 4 точкам (гомография), выпрямление и выравнивание света, контроль качества съёмки; пакетная обработка 40 листов за сеанс со сводкой в XLSX/Markdown (конфликты, вопросы человеку, позиции) |
| `06_Распознавание_сканов_redline.docx` (`docs/06_…md`) | **Распознавание сканов готово:** скан листа с рукописными пометками → локальная VL-модель по API (или офлайн-CV без модели) → типизированные операции; анти-галлюцинационная проверка каждого bbox по чернилам, конфликты «согласовано»/объём против КС-2 уходят человеку, деньги по позициям, 14 MCP-инструментов |
| `05_CAD-модуль_и_MCP.docx` (`docs/05_…md`) | **CAD-модуль готов:** headless DXF на ezdxf, корпоративный шаблон (слои/блоки/шрифты/рамка/штамп), белый список из 7 операций, dry_run/commit с подтверждением человека, сверка объёмов схемы с КС-2 (±5 %), MCP-сервер на 14 инструментов (stdio + http) |
| `04_MVP_ИИ-агент_ПТО.docx` (`docs/04_…md`) | **Отчёт о сборке MVP:** архитектура, результаты прогона, что переиспользовано из OSS и с какой лицензией, соответствие требованиям ТЗ, ограничения, дорожная карта этапа 2 |
| `pto_agent/mcp_server.py` | Точка входа MCP-сервера (`pto-mcp`): фасад над CAD-модулем, настройки из `PTO_MCP_*` |
| `mcp/`, `scripts/install_mcp.py` | Готовые конфиги клиентов (Claude Desktop, Cursor, VS Code, http) и установщик: подставляет пути, не затирает чужие серверы, делает бэкап |
| `Dockerfile`, `mcp/docker-compose.yml` | HTTP-транспорт MCP в контейнере: `/health`, инспектор, журнал аудита на хосте |
| `pyproject.toml` | Пакет и точки входа: `pto-agent`, `pto-cad`, `pto-mcp` |
| `tests/test_mcp_protocol.py` | 15 тестов протокола: рукопожатие, список инструментов, ошибки JSON-RPC, чистота stdout, схемы, http, установщик |
| `pto_agent/cad/external_takeoff.py` | **Внешний обмер по MCP:** клиент (stdio/http, SSE), нормализация чужой схемы к нашей, сверка «чужой обмер ↔ наш DXF/смета», правило «не оценено ≠ 0», лицензионный режим «отдельный сервис» для AGPL |
| `config/external_takeoff.example.yaml` | Конфиг внешнего обмера: транспорт, команда/url, инструмент, лицензия; готовый self-check на нашем же MCP-сервере |
| `tests/test_external_takeoff.py` | 22 теста: фейковые stdio- и http-серверы, SSE, ошибки JSON-RPC и `isError`, нормализация items/positions/lines, отказ на AGPL без подтверждения, совместимость с нашим сервером |
| `pto_agent/cad/` | CAD-модуль: `drawing` (чтение DXF), `template` (корпоративный шаблон), `ops` (белый список, валидация, dry_run/commit), `diff`, `redline` (пометки по расхождениям), `takeoff` (объёмы схемы ↔ акт), `render` (PNG), `mcp_server` (MCP stdio/http) |
| `config/cad_template_corp_A.yaml` | Корпоративный шаблон `corp-A/v4`: 8 слоёв, белый список блоков, стили, поля штампа, масштаб 1:100, допуск сверки ±5 % |
| `drawings/`, `scripts/make_demo_dxf.py` | Демо-чертёж A1 с заложенными дефектами оформления и машиночитаемыми объёмами; генератор + PNG-предпросмотр |
| `out/cad/` | План операций, журналы версий и аудита, выпущенная схема (DXF+PNG+манифест), предпросмотр «лист + фрагмент» |
| `pto_agent/cad/vision/` | Распознавание сканов: `engine` (тайлы, вызов модели, проверки), `providers` (OpenAI-совместимый API, лог вызовов), `prompt`+`schema` (версионированный промпт и строгая схема), `calibrate` (скан ↔ чертёж), `imaging` (маски чернил), `reference`+`detect_offline` (офлайн-CV), `mock` (заглушка VL-модели), `plan` (пометки → операции, конфликты с ПТО) |
| `drawings/redline_scan.png`, `data/demo/redline_truth.json`, `data/demo/redline_texts.json` | Демо-скан A1 с 5 рукописными пометками, эталон разметки и тексты, введённые инженером |
| `scripts/make_demo_redline.py` | Генератор демо-скана: шум, поворот, поля бумаги — и эталонных пометок |
| `pto_agent/cad/vision/perspective.py` | Фото с телефона: гомография по 4 точкам, выпрямление листа, выравнивание света, контроль качества снимка |
| `pto_agent/portfolio.py` | Портфель актов: покрытие проверкой, свод по актам и месяцам, сверка акта со сметой, отчёты `portfolio.xlsx/md/json` и диаграмма |
| `scripts/make_demo_retro.py` | Ретро-выпуск по прошлым актам: сканы + эталон пометок + сводка ПТО периода акта (сверка с количествами того акта, а не с текущим журналом) |
| `data/demo/retro/` | Сканы, эталон и манифесты ретро-выпуска: 15 листов по актам КС-2 №35 и №37 |
| `data/demo/acts/acts.json` | Реестр актов объекта (КС-2 №35, №37, №38): суммы, статус проверки, ссылка на прогон выпуска; собирается `scripts/make_demo_acts.py` |
| `pto_agent/impact.py` | Стоимость споров: правила перевода расхождений в рубли, дедупликация по позиции, отчёты XLSX/Markdown и диаграмма «где деньги» |
| `pto_agent/cad/vision/batch.py` | Пакетная обработка выпуска: манифест, кеш по хэшу скана, изоляция ошибок листа, сводка XLSX/Markdown/JSON |
| `drawings/redline_photo.jpg`, `data/demo/photo_anchors.json`, `data/demo/photo_truth.json` | Демо-фото «с телефона» 2600×1950, 4 опорные точки и эталон пометок |
| `data/demo/batch/` | Демо-выпуск: 4 чертежа комплекта, 40 сканов, эталоны, тексты и `manifest.json` |
| `scripts/make_demo_photo.py`, `scripts/make_demo_batch.py`, `scripts/demo_common.py` | Генераторы демо-фото и демо-выпуска (общий рендер и рисование пометок) |
| `tests/test_photo_batch.py` | 24 теста: гомография, отказы на плохих точках, равенство «фото = скан», качество съёмки, пакет, кеш, сводка, отчёты |
| `tests/test_vision.py` | 24 теста распознавания: схема ответа, калибровка, mock-сервер, анти-галлюцинация, тексты инженера, конфликты с ПТО, MCP |
| `tests/test_cad.py` | 24 теста CAD: DWG-отказ, проверка шаблона, белый список, HITL, diff, takeoff, MCP (включая stdio-процесс) |
| `pto_agent/` | Пакет: `parsers` → `matching` → `reconcile` → `export`/`report`, `norms` (RAG с проверкой цитаты), `audit` (hash-chain), `llm` (маршрутизация по классу данных), `cli` |
| `scripts/make_demo_data.py`, `data/demo/` | Генератор и сам демо-набор + `expected.json` — эталон расхождений |
| `scripts/check_expected.py` | Сверка прогона с эталоном `expected.json`: что найдено, что пропущено, что лишнее |
| `tests/test_mvp.py` | 17 тестов: полнота выявления, дубли, единицы, арифметика, цитаты, применимость редакций, хэш-цепочка, маршрутизация моделей, экспорт КС-2 |
| `out/` | Результаты прогона: `ks2_draft.xlsx`, `discrepancies.xlsx/.csv`, `remainder.xlsx`, `report.html`, `norm_answer.json`, `audit.jsonl`, `summary.json`, `approval.json` |
| `docs/OSS_SBOM.csv` | SBOM: компонент, версия, лицензия, тип использования, в поставке да/нет |
| `data/reference/analogue_projects.json` | Смежные проекты: репозиторий, лицензия, звёзды, «что берём / что не берём», тип интеграции; звёзды и лицензии сверяются с GitHub API |
| `scripts/check_analogues.py` | Проверка списка и лицензионной политики: `--offline` в CI, без флага — сверка с GitHub, `--write` — обновить данные; код возврата 2 при попытке «копировать AGPL» |
| `tests/test_analogues.py` | 10 тестов политики: AGPL и проекты без лицензии никогда не «можно копировать», у MIT-проектов описано, что берём, подмена флага ломает сборку |
| `requirements-pto.txt` | Зависимости с лицензиями; запрещённые (AGPL/GPL, без лицензии) отмечены явно |

## 1. Документы

| Файл | Что внутри | Кому |
|---|---|---|
| `ТЗ_ИИ-агент_ПТО_v1.2.docx` | Полное ТЗ: 17 разделов + приложения A–F. Готово к согласованию | ИТ, ПТО, закупки |
| `ТЗ_ИИ-агент_ПТО_v1.2.md` | То же в markdown — для правок в git/Confluence | команда |
| `01_Golden_set_и_методика_разметки.docx` | ТЗ на эталонную выборку: состав 360 единиц, схемы разметки, метрики с порогами, регламент регрессии, критерии приёмки | ПТО + методолог |
| `Видео_презентация_ИИ-агент_ПТО.mp4` | **Видео-презентация продукта, 3:36, 1080p, с озвучкой.** 10 слайдов: проблема → 4 сценария MVP → как работает → прозрачность и остатки → исполнительные схемы → безопасность → ИИ по API → план и бюджет → что нужно для старта | руководство, инвесторы |
| `Презентация_ИИ-агент_ПТО.pptx` | 14 слайдов для защиты бюджета: проблема → scope → архитектура → CAD → безопасность → рынок/OSS → план → бюджет → метрики → риски → ask | руководство |
| `Дашборд_руководителя_ПТО.html` | **Экран руководителя.** Стадии проекта (план/факт), остаточные объёмы по работам, деньги по договору и **деньги под риском** (сколько стоит каждый спор с актом — из сводки выпуска), стоимость и качество ИИ, журнал действий, список решений. Каждая цифра — с адресом источника. Открывается в браузере, без внешних зависимостей | руководство, ПТО |
| `03_Подбор_подрядчика.docx` | Шорт-лист исполнителей: 15 команд по GitHub-следу (проверка 02.11.2026), скоринг по 8 критериям, 4 модели контракта, рекомендуемая гибридная конфигурация, красные флаги | закупки, ИТ |
| `Шорт-лист_подрядчиков_ПТО.xlsx` | Скоринг-матрица с формулами (веса редактируются), 25 вопросов RFP с «хорошими» и «плохими» ответами, чек-лист проверки GitHub-профиля с API-командами | закупки, ИТ |
| `04_MVP_ИИ-агент_ПТО.docx` | **MVP собран своими силами:** архитектура прототипа, 7 найденных расхождений, SBOM и лицензии, соответствие AC-TR/AC-LLM, ограничения, план этапа 2 | ИТ, ПТО |
| `02_Прозрачность_и_ИИ_по_API.docx` | ТЗ, разделы 18–19: прозрачность, стадии, остатки, отчёт руководителю; подключение ИИ-моделей по API (маршрутизация, бюджеты, fallback, приёмка модели) | ИТ, ИБ, ПТО |
| `config/llm_providers.yaml` | 5 провайдеров (2 локальных + 2 РФ-облака + внешний запрещённый), 5 правил маршрутизации по классу данных, бюджеты, fallback, привязка промптов, пороги приёмки новой модели | ИТ, ИБ |
| `db/002_transparency_llm.sql` | Миграция: стадии и контрольные точки, снимки остатков и прогресса, платежи, провайдеры и журнал вызовов моделей, бюджеты, отчёты; представления `v_work_remainder`, `v_object_progress`, `v_finance_position`, `v_stage_status`, `v_llm_cost_daily`, `v_trace_chain`; триггер запрета выноса данных | разработка |

## 2. Промпты и контракты инструментов

| Файл | Назначение |
|---|---|
| `prompts/01_ks2_reconciliation.md` | «Сметчик»: КС-2, 7 классов расхождений, JSON-схема ответа |
| `prompts/02_aosr_generation.md` | «Документатор»: АОСР, комплектность ИД, чек-лист замечаний |
| `prompts/03_norm_control_rag.md` | «Нормативщик»: ответ только с цитатой, правила конфликтов норм, регрессионные тесты |
| `prompts/04_asbuilt_scheme_cad.md` | «Чертёжник»: redline → типизированные CAD-операции, белый список, блокирующие валидации |
| `prompts/tools_schema.json` | 17 инструментов с JSON-схемами, guardrails, политикой логирования и лимитами |

## 2а. Видео и его исходники

| Файл | Назначение |
|---|---|
| `video/narration.json` | Сценарий: текст каждой реплики + содержание слайда (единый источник правды) |
| `video/build_slides.py` | Рендер 10 слайдов 1920×1080 (Pillow, DejaVu, без внешних ресурсов) |
| `video/make_video.py` | Сборка mp4: слайд + озвучка → сегмент → склейка (ffmpeg из imageio-ffmpeg) |
| `video/slides/*.png` | Готовые кадры — можно использовать в PowerPoint или на печать |

Пересобрать видео целиком:

```bash
python3 video/build_slides.py
python3 video/make_video.py
```

## 3. Данные и API

| Файл | Назначение |
|---|---|
| `db/schema.sql` | PostgreSQL 16: 40+ таблиц — объекты, договоры, сметы, журналы, КС-2/КС-3, ИД и связи, комплектность, нормативы с редакциями, CAD-операции, задачи агентов, append-only журнал аудита с хэш-цепочкой, RLS, 3 представления. Проверено парсером PostgreSQL |
| `api/openapi.yaml` | OpenAPI 3.1, 33 пути: КС-2/КС-3, сверка, комплектность, нормы, CAD (dry_run/commit), выпуск схем, задачи, аудит. Проверено `openapi-spec-validator`. Теги `reports` (отчёт руководителю, остатки, финансы, стадии) и `llm` (провайдеры, стоимость, предпросмотр маршрутизации) |
| `golden_set/templates/*.jsonl` | Шаблоны разметки GS-01…GS-05 |
| `golden_set/templates/passport.csv` | Паспорт выборки (инвентаризация) |
| `golden_set/tools/validate_gs.py` | Автопроверки разметки: JSON, обязательные поля, арифметика КС-2, допустимые типы ошибок, спаны, bbox |

## 4. Как пользоваться

```bash
# пересобрать ТЗ или ТЗ на golden set после правок markdown
python3 docs/build_docx.py "ТЗ_ИИ-агент_ПТО_v1.2.md" "ТЗ_ИИ-агент_ПТО_v1.2.docx"
python3 docs/build_docx.py "docs/01_Golden_set_и_методика_разметки.md" "01_Golden_set_и_методика_разметки.docx"

# пересобрать презентацию
python3 docs/build_pptx.py

# пересобрать дашборд руководителя (числа считаются из одного массива данных)
python3 docs/build_dashboard.py

# проверить файлы разметки перед сдачей
python3 golden_set/tools/validate_gs.py golden_set/templates/

# CAD-модуль: чертёж → проверка шаблона → пометки → версия
python3 scripts/make_demo_dxf.py
python3 -m pto_agent cad validate --file drawings/korpus2_plan_v1.dxf
python3 -m pto_agent cad plan     --file drawings/korpus2_plan_v1.dxf --summary out/summary.json
python3 -m pto_agent cad apply    --file drawings/korpus2_plan_v1.dxf --plan out/cad/plan.json --mode dry_run
python3 -m pto_agent cad apply    --file drawings/korpus2_plan_v1.dxf --plan out/cad/plan.json \
        --mode commit --confirm-by "Иванов И.И."
python3 -m pto_agent mcp --transport http --port 8765     # MCP-инспектор и JSON-RPC на /mcp

# распознавание скана с пометками: скан → операции → проверка → commit с подписью
python3 scripts/make_demo_redline.py
python3 -m pto_agent cad scan --scan drawings/redline_scan.png \
        --file drawings/korpus2_plan_v1.dxf --engine vlm --json out/cad/scan.json
python3 -m pto_agent cad redline-from-scan --scan drawings/redline_scan.png \
        --file drawings/korpus2_plan_v1.dxf --engine offline \
        --texts data/demo/redline_texts.json --mode plan_only

# фото с телефона: 4 угла листа → выпрямление → пометки
python3 scripts/make_demo_photo.py
python3 -m pto_agent cad scan --scan drawings/redline_photo.jpg \
        --file drawings/korpus2_plan_v1.dxf --engine offline \
        --anchors data/demo/photo_anchors.json --rectified out/cad/photo_rectified.png

# выпуск целиком: 40 листов за сеанс, сводка в out/cad/batch/
python3 scripts/make_demo_batch.py
python3 -m pto_agent cad scan-batch --manifest data/demo/batch/manifest.json \
        --engine vlm --out out/cad/batch
python3 -m pto_agent cad impact --batch out/cad/batch/batch_summary.json \
        --summary out/summary.json --out out/cad/impact --with-background
python3 -m pto_agent cad impact --acts data/demo/acts/acts.json \
        --summary out/summary.json --out out/cad/portfolio
# внешний обмер по MCP (чужой сервер меряет, мы сверяем) — self-check на своём сервере
python3 -m pto_agent cad takeoff-external --config config/external_takeoff.example.yaml \
        --pdf drawings/korpus2_plan_v1.dxf --file drawings/korpus2_plan_v1.dxf --out out/cad/external
python3 scripts/make_demo_retro.py && python3 scripts/make_demo_acts.py   # ретро-выпуск

# полный цикл MVP агента (сверка → черновик КС-2 → отчёт → аудит)
python3 -m pto_agent run --data data/demo --out out
python3 -m pytest tests -q
```

Тесты, которым нужны тяжёлые демо-артефакты (сканы выпуска, ретро-выпуски, `out/cad/batch*`),
помечены `skipif` и просто пропускаются, если артефактов нет: свежий клон даёт **0 падений**
(132 passed / 24 skipped после `make_demo_redline.py`), а джоб `e2e` в CI собирает весь набор
и прогоняет все тесты на реальных отчётах.

```bash

# поднять БД (после установки расширений pgcrypto, vector, pg_trgm)
psql -d pto_agent -f db/schema.sql
psql -d pto_agent -f db/002_transparency_llm.sql
```

---

## 5. Прозрачность и ИИ по API — зафиксированные правила

1. **Две цифры вместо одной:** «выполнено» (физический факт по журналам) и «закрыто» (подписанный акт). Работы без документов не списываются в остаток — они висят как «готово к предъявлению».
2. **Остаток = смета − закрыто подписанными КС-2 − выполнено, но не закрыто актом.** Одна формула на всю систему, сумма по видам работ обязана сходиться с итогом до рубля.
3. **Каждая цифра отчёта имеет `trace_ref`** — раскрывается до документа, листа и строки. Журнал append-only с хэш-цепочкой, изменение записей невозможно технически.
4. **Модель — сменный компонент.** Подключение через единый OpenAI-совместимый шлюз; данные классов «коммерческая тайна» и «ПДн» обрабатываются только в контуре заказчика — блокировка на уровне шлюза **и** триггера БД.
5. **Бюджет ИИ с жёстким стопом:** при 80 % лимита — предупреждение, при 100 % — внешние вызовы прекращаются, система продолжает работать на локальных моделях.

## 6. Пять решений, которые зафиксированы в ТЗ

1. **MVP = 4 сценария, а не 18.** КС-2/КС-3, сверка объёмов, комплектность ИД, АОСР. Исполнительные схемы — этап «CAD + 2.0» (июнь–декабрь 2027).
2. **Арифметику делает код, а не модель** (`calc_totals` + принцип детерминизма расчётов).
3. **Ответ без цитаты запрещён.** `cite_verify` блокирует выдачу, если цитата не найдена в указанном пункте указанной редакции. Цель — ≤ 0,5 % выдуманных ссылок.
4. **CAD-модуль: headless-first (ezdxf + ODA), live-мост — опция.** Плата — потеря объектов при конвертации, отсюда обязательное сравнение количества объектов до/после и запрет выпуска при расхождении.
5. **Собственная сборка, а не форк «готового каркаса».** Аудит GitHub 02.11.2026: у `construction-ai-core` **нет лицензии** (только идеи), `AutoGAD` под **AGPL-3.0** (не для закрытого продукта). Рабочая база: `ai-pmo-skills` (MIT), `DDC_Skills` (MIT), `Autocad-MCP` (MIT), `cad-ai-agent` (Apache-2.0), `ezdxf` (MIT).

---

## 7. Ближайшие шаги (этап 0, 10.11 – 05.12.2026)

1. Согласовать ТЗ v1.2 и ТЗ на golden set, назначить владельца выборки.
2. Собрать 50–100 пакетов документов + NDA/обезличивание (Приложение E ТЗ, п.2.2 ТЗ на golden set).
3. Разметить: 30 актов КС-2, 20 пакетов ИД, 200 нормативных вопросов, 20 redline-кейсов, 60 документов на извлечение полей.
4. Провести базовый хронометраж «как есть» — точка отсчёта для метрик и для критериев остановки.
5. Параллельно — 2 демо вендорских решений на своих данных (страховка на случай «купить вместо делать»).

## 8. Что можно сделать следующим

- Схема БД «как есть» → миграции Alembic/Flyway и seed-данные для демо-стенда.
- Реализовать `calc_totals` + `reconcile_volumes` как отдельный сервис с тестами на golden set.
- ТЗ на пилот: регламент работы инженера с агентом, инструкция пользователя, форма журнала замечаний.
- Расчёт ROI под фактические цифры компании (ФОТ ПТО, объём ИД, штрафы за просрочку).
- Прототип CAD-пайплайна на ezdxf: чтение DWG/DXF, список слоёв, применение 3 операций, diff-растр.

## 9. Смежные проекты и что мы у них берём

Проверенный список открытых проектов по сметам и обмерам — в
[`docs/11_Смежные_проекты_и_лицензии.md`](docs/11_Смежные_проекты_и_лицензии.md),
машиночитаемо — `data/reference/analogue_projects.json` (звёзды и лицензии сверены с GitHub API 2026-11-03).

| Проект | Лицензия | Что берём |
|---|---|---|
| [bidwright](https://github.com/braedonsaunders/bidwright) | AGPL-3.0 | декомпозицию intake → takeoff → pricing → **review** → quote; только как идею |
| [opentakeoff](https://github.com/Kentucky-ai/opentakeoff) | AGPL-3.0 | обмер как **внешний MCP-сервер**, который поднимает сам пользователь — наш код остаётся MIT |
| [orama-core](https://github.com/oramaai/orama-core) | MIT | RAG по расценкам с обязательной ссылкой на источник, схему «код → единица → объём → цена» |
| [estimaite-mcp](https://github.com/nickpanagios/estimaite-mcp) | MIT | схему инструментов `upload → measure → price → export` для наших MCP-описаний |
| [DDC_Skills](https://github.com/datadrivenconstruction/DDC_Skills_for_AI_Agents_in_Construction) | MIT | роли QTO / Pricing / Validation и формат skill «когда вызывать, входы, выходы, критерий успеха» |
| [OpenConstructionEstimate](https://github.com/datadrivenconstruction/OpenConstructionEstimate-DDC-CWICR) | NOASSERTION | базу ГЭСН/ФЕР/ТЕР для позиций, которых нет в смете — после ручной проверки условий |

Правило простое: **лицензия сильнее звёзд**. В MIT-поставку не попадает ни строчки AGPL-кода
и кода без лицензии; такие проекты либо остаются внешним процессом, либо дают только идею.
Проверяется автоматически:

Шаг 1 дорожной карты сделан: адаптер `pto_agent/cad/external_takeoff.py`, CLI
`python3 -m pto_agent cad takeoff-external` и 15-й MCP-инструмент `external_takeoff` — агент сам
зовёт чужой обмер и сверяет его с нашим чертежом; чужой код остаётся за границей протокола.

```bash
python3 scripts/check_analogues.py --offline    # структура и политика, без сети (то же делает CI)
python3 scripts/check_analogues.py              # сверить звёзды и лицензии с GitHub API
python3 -m pytest tests/test_analogues.py -q     # 10 тестов политики
```
