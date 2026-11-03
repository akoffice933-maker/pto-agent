-- ============================================================================
--  Миграция 002: прозрачность, стадии проекта, остаточные объёмы,
--  финансовая позиция, подключение ИИ-моделей по API.
--  Применяется после db/schema.sql. PostgreSQL 16+.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Стадии внедрения (не путать с этапами объекта)
-- ---------------------------------------------------------------------------

CREATE TABLE project_stage (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001',
    code            text NOT NULL,               -- 'stage0', 'mvp', 'pilot', 'v1', 'v2'
    name            text NOT NULL,
    scope           text,
    planned_from    date NOT NULL,
    planned_to      date NOT NULL,
    actual_from     date,
    actual_to       date,
    progress_pct    numeric(5,2) NOT NULL DEFAULT 0 CHECK (progress_pct BETWEEN 0 AND 100),
    status          text NOT NULL DEFAULT 'planned'
                    CHECK (status IN ('planned','in_progress','at_risk','done','cancelled')),
    fact_note       text,                        -- «что реально сделано» человеческим языком
    next_step       text,
    owner_id        uuid REFERENCES app_user(id),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, code),
    CHECK (planned_to >= planned_from)
);

-- Контрольные точки этапов с критериями приёмки и фактическим результатом
CREATE TABLE stage_checkpoint (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    stage_id        uuid NOT NULL REFERENCES project_stage(id) ON DELETE CASCADE,
    code            text NOT NULL,
    title           text NOT NULL,
    due_date        date NOT NULL,
    criterion       text NOT NULL,               -- измеримый критерий (метрика + порог)
    result_value    text,
    result_status   text NOT NULL DEFAULT 'pending'
                    CHECK (result_status IN ('pending','passed','failed','waived')),
    verified_by     uuid REFERENCES app_user(id),
    verified_at     timestamptz,
    UNIQUE (stage_id, code)
);

-- История прогресса этапов (для графика «план/факт» и честной оценки темпа)
CREATE TABLE stage_progress_snapshot (
    id              bigserial PRIMARY KEY,
    stage_id        uuid NOT NULL REFERENCES project_stage(id) ON DELETE CASCADE,
    snapshot_date   date NOT NULL,
    progress_pct    numeric(5,2) NOT NULL,
    open_questions  int NOT NULL DEFAULT 0,
    risks_count     int NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stage_id, snapshot_date)
);

-- ---------------------------------------------------------------------------
-- 2. Остаточные объёмы: снимки + представление
--    Логика: остаток = смета − подписано КС-2 − выполнено, но не закрыто
-- ---------------------------------------------------------------------------

CREATE TABLE work_remainder_snapshot (
    id              bigserial PRIMARY KEY,
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    work_type_id    uuid NOT NULL REFERENCES work_type(id),
    snapshot_date   date NOT NULL,
    unit_code       text NOT NULL REFERENCES unit(code),
    qty_estimate    numeric(16,4) NOT NULL,
    qty_closed      numeric(16,4) NOT NULL,      -- закрыто подписанными КС-2
    qty_wip         numeric(16,4) NOT NULL,      -- выполнено по журналам, не закрыто актом
    qty_remainder   numeric(16,4) GENERATED ALWAYS AS (qty_estimate - qty_closed - qty_wip) STORED,
    amount_estimate numeric(18,2) NOT NULL,
    amount_closed   numeric(18,2) NOT NULL,
    amount_wip      numeric(18,2) NOT NULL,
    amount_remainder numeric(18,2) GENERATED ALWAYS AS (amount_estimate - amount_closed - amount_wip) STORED,
    progress_pct    numeric(5,2) GENERATED ALWAYS AS (
                        CASE WHEN amount_estimate = 0 THEN 0
                             ELSE ROUND(100 * amount_closed / amount_estimate, 2) END) STORED,
    doc_coverage_pct numeric(5,2),               -- доля закрытых объёмов, обеспеченных ИД
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (object_id, work_type_id, snapshot_date)
);
CREATE INDEX ix_remainder_obj_date ON work_remainder_snapshot(object_id, snapshot_date DESC);

-- Сводный снимок по объекту (для дашборда и отчёта руководителю)
CREATE TABLE object_progress_snapshot (
    id              bigserial PRIMARY KEY,
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    snapshot_date   date NOT NULL,
    amount_contract numeric(18,2) NOT NULL,
    amount_estimate numeric(18,2) NOT NULL,
    amount_closed   numeric(18,2) NOT NULL,
    amount_wip      numeric(18,2) NOT NULL,
    amount_received numeric(18,2) NOT NULL,
    amount_submitted numeric(18,2) NOT NULL,
    progress_pct    numeric(5,2) NOT NULL,
    progress_with_wip_pct numeric(5,2) NOT NULL,
    doc_coverage_pct numeric(5,2),
    critical_discrepancies int NOT NULL DEFAULT 0,
    positions_without_docs int NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (object_id, snapshot_date)
);

-- ---------------------------------------------------------------------------
-- 3. Финансовая позиция по договору (получено / предъявлено / дебиторка)
-- ---------------------------------------------------------------------------

CREATE TABLE payment_record (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contract_id     uuid NOT NULL REFERENCES contract(id),
    ks3_id          uuid REFERENCES act_ks3(id),
    kind            text NOT NULL CHECK (kind IN ('advance','payment','retention','refund','offset')),
    amount          numeric(18,2) NOT NULL,
    paid_at         date NOT NULL,
    payer_org_id    uuid REFERENCES organization(id),
    bank_doc_number text,
    file_id         uuid REFERENCES file_blob(id),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_payment_contract ON payment_record(contract_id, paid_at);

-- ---------------------------------------------------------------------------
-- 4. Подключение ИИ-моделей по API (зеркало config/llm_providers.yaml)
-- ---------------------------------------------------------------------------

CREATE TABLE llm_provider (
    id              text PRIMARY KEY,            -- 'local-qwen-32b'
    kind            text NOT NULL CHECK (kind IN ('vllm','ollama','openai_compatible','gigachat','yandexgpt')),
    base_url        text NOT NULL,
    model           text NOT NULL,
    context_window  int,
    vision          boolean NOT NULL DEFAULT false,
    capabilities    text[] NOT NULL DEFAULT '{}',
    price_per_1k_in_rub  numeric(10,4) NOT NULL DEFAULT 0,
    price_per_1k_out_rub numeric(10,4) NOT NULL DEFAULT 0,
    data_classes    text[] NOT NULL DEFAULT '{}',-- какие классы данных допустимы
    residency       text NOT NULL CHECK (residency IN ('on_premise','rf_cloud','outside_rf')),
    is_enabled      boolean NOT NULL DEFAULT true,
    is_fallback_only boolean NOT NULL DEFAULT false,
    config_json     jsonb NOT NULL DEFAULT '{}',
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE llm_call_log (
    id              bigserial PRIMARY KEY,
    task_id         uuid REFERENCES agent_task(id) ON DELETE SET NULL,
    step_id         uuid REFERENCES agent_step(id) ON DELETE SET NULL,
    provider_id     text NOT NULL REFERENCES llm_provider(id),
    model           text NOT NULL,
    prompt_id       text,
    prompt_version  text,
    intent          text,
    data_class      text NOT NULL CHECK (data_class IN ('public','internal','commercial_secret','personal')),
    routing_rule    text,                        -- какое правило сработало
    tokens_in       int NOT NULL DEFAULT 0,
    tokens_out      int NOT NULL DEFAULT 0,
    cost_rub        numeric(12,4) NOT NULL DEFAULT 0,
    latency_ms      int,
    http_status     int,
    fallback_used   boolean NOT NULL DEFAULT false,
    cache_hit       boolean NOT NULL DEFAULT false,
    json_schema_ok  boolean,
    validators_ok   boolean,
    validators_json jsonb,
    error_code      text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_llm_call_created ON llm_call_log(created_at DESC);
CREATE INDEX ix_llm_call_provider ON llm_call_log(provider_id, created_at DESC);
CREATE INDEX ix_llm_call_task ON llm_call_log(task_id);

-- Политика запрета выноса данных: проверка перед записью факта вызова
CREATE OR REPLACE FUNCTION llm_call_check_residency() RETURNS trigger AS $$
DECLARE
    allowed text[];
    res     text;
BEGIN
    SELECT data_classes, residency INTO allowed, res
    FROM llm_provider WHERE id = NEW.provider_id;

    IF NOT (NEW.data_class = ANY (allowed)) THEN
        RAISE EXCEPTION 'provider % не допускает класс данных %', NEW.provider_id, NEW.data_class;
    END IF;
    IF res = 'outside_rf' THEN
        RAISE EXCEPTION 'вызов внешнего провайдера запрещён политикой ИБ';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_llm_call_residency
    BEFORE INSERT ON llm_call_log
    FOR EACH ROW EXECUTE FUNCTION llm_call_check_residency();

-- Бюджеты: дневной/месячный лимит и деградация на локальные модели
CREATE TABLE llm_budget (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope           text NOT NULL CHECK (scope IN ('global','user','task')),
    scope_id        text,
    period_start    date NOT NULL,
    period_end      date NOT NULL,
    limit_rub       numeric(12,2) NOT NULL,
    spent_rub       numeric(12,2) NOT NULL DEFAULT 0,
    hard_stop       boolean NOT NULL DEFAULT true,
    UNIQUE (scope, scope_id, period_start)
);

-- ---------------------------------------------------------------------------
-- 5. Отчёт руководителю: выпуск, получатели, доступ
-- ---------------------------------------------------------------------------

CREATE TABLE report_subscription (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_code     text NOT NULL,               -- 'management_daily','remainder_weekly'
    object_id       uuid REFERENCES construction_object(id),
    user_id         uuid NOT NULL REFERENCES app_user(id),
    channel         text NOT NULL CHECK (channel IN ('email','telegram','portal')),
    schedule_cron   text,                        -- '0 7 * * 1-5'
    is_active       boolean NOT NULL DEFAULT true,
    UNIQUE (report_code, object_id, user_id, channel)
);

CREATE TABLE report_export (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_code     text NOT NULL,
    object_id       uuid REFERENCES construction_object(id),
    period_from     date,
    period_to       date,
    format          text NOT NULL CHECK (format IN ('html','pdf','xlsx')),
    file_id         uuid REFERENCES file_blob(id),
    payload_hash    char(64),                    -- хэш данных, из которых собран отчёт
    requested_by    uuid REFERENCES app_user(id),
    delivered_to    text[],
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 6. Представления: остатки, прогресс, финансы, этапы, стоимость ИИ
-- ---------------------------------------------------------------------------

-- Остатки в разрезе видов работ (на текущую дату, из оперативных данных)
CREATE OR REPLACE VIEW v_work_remainder AS
WITH est AS (
    SELECT e.object_id,
           ei.unit_code,
           SUM(ei.qty)                      AS qty_estimate,
           SUM(ei.qty * ei.price * ei.coefficient) AS amount_estimate
    FROM estimate_item ei
    JOIN estimate e ON e.id = ei.estimate_id AND e.status = 'approved'
    GROUP BY e.object_id, ei.unit_code
),
closed AS (
    SELECT a.object_id, ii.unit_code,
           SUM(ii.qty)      AS qty_closed,
           SUM(ii.amount)   AS amount_closed
    FROM act_ks2_item ii
    JOIN act_ks2 a ON a.id = ii.act_id AND a.status IN ('approved','signed')
    GROUP BY a.object_id, ii.unit_code
),
wip AS (
    SELECT j.object_id, j.unit_code, SUM(j.qty) AS qty_wip
    FROM journal_entry j
    WHERE NOT EXISTS (
        SELECT 1 FROM act_ks2_item ii
        JOIN act_ks2 a ON a.id = ii.act_id
        WHERE a.object_id = j.object_id
          AND a.status IN ('approved','signed')
          AND ii.estimate_item_id = j.estimate_item_id
    )
    GROUP BY j.object_id, j.unit_code
)
SELECT e.object_id,
       e.unit_code,
       e.qty_estimate,
       COALESCE(c.qty_closed, 0)                    AS qty_closed,
       COALESCE(w.qty_wip, 0)                       AS qty_wip,
       e.qty_estimate - COALESCE(c.qty_closed, 0) - COALESCE(w.qty_wip, 0) AS qty_remainder,
       e.amount_estimate,
       COALESCE(c.amount_closed, 0)                 AS amount_closed,
       e.amount_estimate - COALESCE(c.amount_closed, 0) AS amount_remainder,
       ROUND(100 * COALESCE(c.amount_closed, 0) / NULLIF(e.amount_estimate, 0), 2) AS progress_pct
FROM est e
LEFT JOIN closed c ON c.object_id = e.object_id AND c.unit_code = e.unit_code
LEFT JOIN wip w    ON w.object_id = e.object_id AND w.unit_code = e.unit_code;

-- Прогресс по объекту одним запросом (для дашборда и отчёта)
CREATE OR REPLACE VIEW v_object_progress AS
SELECT o.id AS object_id,
       o.code,
       o.name,
       COALESCE(r.amount_estimate, 0)                        AS amount_estimate,
       COALESCE(r.amount_closed, 0)                          AS amount_closed,
       COALESCE(r.amount_estimate - r.amount_closed, 0)      AS amount_remainder,
       COALESCE(r.progress_pct, 0)                           AS progress_pct,
       (SELECT COUNT(*) FROM discrepancy d
         WHERE d.object_id = o.id AND d.severity = 'critical'
           AND d.human_decision IS NULL)                     AS critical_discrepancies,
       (SELECT COUNT(*) FROM act_ks2_item ii
          JOIN act_ks2 a ON a.id = ii.act_id
         WHERE a.object_id = o.id AND NOT ii.has_backing_docs) AS positions_without_docs
FROM construction_object o
LEFT JOIN (
    SELECT object_id,
           SUM(amount_estimate) AS amount_estimate,
           SUM(amount_closed)   AS amount_closed,
           ROUND(100 * SUM(amount_closed) / NULLIF(SUM(amount_estimate),0), 2) AS progress_pct
    FROM v_work_remainder GROUP BY object_id
) r ON r.object_id = o.id;

-- Финансовая позиция по договору
CREATE OR REPLACE VIEW v_finance_position AS
SELECT c.id AS contract_id,
       c.object_id,
       c.number,
       c.amount_total,
       COALESCE(closed.total_closed, 0)      AS amount_closed,
       COALESCE(sub.submitted, 0)            AS amount_submitted,
       COALESCE(paid.total_paid, 0)          AS amount_received,
       COALESCE(closed.total_closed, 0) - COALESCE(paid.total_paid, 0) AS receivable,
       c.amount_total - COALESCE(closed.total_closed, 0) - COALESCE(sub.submitted, 0) AS amount_to_submit
FROM contract c
LEFT JOIN (
    SELECT contract_id, SUM(total_no_vat) AS total_closed
    FROM act_ks2 WHERE status IN ('approved','signed') GROUP BY contract_id
) closed ON closed.contract_id = c.id
LEFT JOIN (
    SELECT contract_id, SUM(total_no_vat) AS submitted
    FROM act_ks2 WHERE status IN ('on_review') GROUP BY contract_id
) sub ON sub.contract_id = c.id
LEFT JOIN (
    SELECT contract_id, SUM(amount) AS total_paid
    FROM payment_record WHERE kind IN ('payment','advance') GROUP BY contract_id
) paid ON paid.contract_id = c.id;

-- Статус этапов с отклонением от плана в днях
CREATE OR REPLACE VIEW v_stage_status AS
SELECT ps.id,
       ps.code,
       ps.name,
       ps.status,
       ps.progress_pct,
       ps.planned_from,
       ps.planned_to,
       ps.actual_to,
       CASE WHEN ps.status = 'done' THEN (ps.actual_to - ps.planned_to)
            ELSE (CURRENT_DATE - ps.planned_to) END AS schedule_delta_days,
       ps.fact_note,
       ps.next_step,
       (SELECT COUNT(*) FROM stage_checkpoint sc
         WHERE sc.stage_id = ps.id AND sc.result_status = 'failed') AS failed_checkpoints,
       (SELECT COUNT(*) FROM stage_checkpoint sc
         WHERE sc.stage_id = ps.id AND sc.result_status = 'pending'
           AND sc.due_date < CURRENT_DATE) AS overdue_checkpoints
FROM project_stage ps;

-- Стоимость и качество ИИ по дням и провайдерам
CREATE OR REPLACE VIEW v_llm_cost_daily AS
SELECT date_trunc('day', l.created_at)::date AS day,
       l.provider_id,
       COUNT(*)                                AS calls,
       SUM(l.tokens_in)                        AS tokens_in,
       SUM(l.tokens_out)                       AS tokens_out,
       SUM(l.cost_rub)                         AS cost_rub,
       ROUND(AVG(l.latency_ms))                AS avg_latency_ms,
       ROUND(100.0 * COUNT(*) FILTER (WHERE l.json_schema_ok) / NULLIF(COUNT(*),0), 2) AS schema_ok_pct,
       COUNT(*) FILTER (WHERE l.fallback_used) AS fallbacks,
       COUNT(*) FILTER (WHERE l.error_code IS NOT NULL) AS errors
FROM llm_call_log l
GROUP BY 1, 2;

-- Прослеживаемость: любая цифра отчёта → документ-источник
CREATE OR REPLACE VIEW v_trace_chain AS
SELECT t.id AS task_id,
       t.object_id,
       t.agent,
       t.intent,
       t.status,
       s.step_no,
       s.tool,
       s.ok,
       l.provider_id,
       l.model,
       l.prompt_id,
       l.prompt_version,
       l.cost_rub,
       s.created_at
FROM agent_task t
LEFT JOIN agent_step s ON s.task_id = t.id
LEFT JOIN llm_call_log l ON l.task_id = t.id
ORDER BY t.created_at DESC, s.step_no;

COMMENT ON VIEW v_work_remainder IS 'Остаток = смета − закрыто подписанными КС-2 − выполнено по журналам, но не закрыто актом';
COMMENT ON TABLE llm_call_log IS 'Каждый вызов модели: провайдер, промпт-версия, токены, стоимость, результат валидации';
COMMENT ON TABLE project_stage IS 'Стадии внедрения: план/факт, прогресс, честная формулировка «что реально сделано»';
