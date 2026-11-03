-- ============================================================================
--  ИИ-агент ПТО — модель данных (PostgreSQL 16+)
--  Версия 1.0 / 02.11.2026
--  Назначение: объекты, договоры, сметы, объёмы, КС-2/КС-3, документы ИД,
--              комплектность, нормативная база (версионирование), CAD-модуль,
--              задачи агентов и неизменяемый журнал аудита.
--  Принципы: UUID PK, изоляция по organization_id (RLS), мягкое удаление
--            не используется для ИД (только версии + статусы), jsonb для
--            изменяемых структур, FK + CHECK на критичных полях.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";      -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "vector";        -- pgvector: эмбеддинги норм
CREATE EXTENSION IF NOT EXISTS "pg_trgm";       -- нечёткий поиск по наименованиям

-- ---------------------------------------------------------------------------
-- 0. Справочники и организации
-- ---------------------------------------------------------------------------

CREATE TABLE organization (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL CHECK (kind IN ('customer','general_contractor','subcontractor','our_company')),
    name            text NOT NULL,
    inn             char(12),
    kpp             char(9),
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organization(id),
    email           text NOT NULL UNIQUE,
    full_name       text NOT NULL,
    role            text NOT NULL CHECK (role IN (
                        'pto_engineer','lead_engineer','pto_head','estimator',
                        'customer_inspector','admin','auditor','gip')),
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE unit (
    code            text PRIMARY KEY,            -- 'm3', 'm2', 'm', 'pcs', 't', 'set'
    name            text NOT NULL,
    okei_code       text
);

CREATE TABLE work_type (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code            text NOT NULL UNIQUE,
    name            text NOT NULL,
    parent_id       uuid REFERENCES work_type(id),
    classifier_code text,                        -- код по классификатору видов работ
    requires_scheme boolean NOT NULL DEFAULT false,
    is_active       boolean NOT NULL DEFAULT true
);

-- ---------------------------------------------------------------------------
-- 1. Объекты и договоры
-- ---------------------------------------------------------------------------

CREATE TABLE construction_object (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organization(id),   -- наш генподрядчик
    customer_org_id uuid REFERENCES organization(id),
    code            text NOT NULL,
    name            text NOT NULL,
    address         text,
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('design','active','suspended','closed')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, code)
);

CREATE TABLE contract (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    contractor_org_id uuid NOT NULL REFERENCES organization(id),
    number          text NOT NULL,
    signed_at       date NOT NULL,
    amount_total    numeric(18,2) NOT NULL CHECK (amount_total >= 0),
    vat_rate        numeric(5,2) NOT NULL DEFAULT 20.00,
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('draft','active','suspended','closed')),
    UNIQUE (object_id, number)
);

-- ---------------------------------------------------------------------------
-- 2. Файловое хранилище (S3/MinIO), версии и целостность
-- ---------------------------------------------------------------------------

CREATE TABLE file_blob (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    storage_key     text NOT NULL UNIQUE,        -- ключ в S3
    original_name   text NOT NULL,
    mime            text NOT NULL,
    size_bytes      bigint NOT NULL CHECK (size_bytes >= 0),
    sha256          char(64) NOT NULL,
    uploaded_by     uuid REFERENCES app_user(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    antivirus_status text NOT NULL DEFAULT 'pending'
                    CHECK (antivirus_status IN ('pending','clean','infected','error'))
);
CREATE INDEX ix_file_blob_sha ON file_blob(sha256);

CREATE TABLE document_scan (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id         uuid NOT NULL REFERENCES file_blob(id),
    page_no         int NOT NULL CHECK (page_no > 0),
    has_text_layer  boolean NOT NULL DEFAULT false,
    ocr_confidence  numeric(4,3) CHECK (ocr_confidence BETWEEN 0 AND 1),
    layout_json     jsonb,                       -- блоки текста/таблиц с bbox
    UNIQUE (file_id, page_no)
);

-- ---------------------------------------------------------------------------
-- 3. Сметы и позиции
-- ---------------------------------------------------------------------------

CREATE TABLE estimate (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    contract_id     uuid REFERENCES contract(id),
    version         int NOT NULL DEFAULT 1,
    source_system   text NOT NULL CHECK (source_system IN ('grand_smeta','smeta_ru','excel','1c','manual')),
    source_file_id  uuid REFERENCES file_blob(id),
    basis           text,                        -- ФЕР / ТЕР / коммерческие
    status          text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','approved','superseded')),
    approved_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (object_id, contract_id, version)
);

CREATE TABLE estimate_item (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    estimate_id     uuid NOT NULL REFERENCES estimate(id) ON DELETE CASCADE,
    parent_id       uuid REFERENCES estimate_item(id),      -- иерархия разделов
    line_no         int NOT NULL,
    code            text,                                   -- шифр расценки ФЕР/ТЕР
    name            text NOT NULL,
    unit_code       text NOT NULL REFERENCES unit(code),
    qty             numeric(16,4) NOT NULL CHECK (qty >= 0),
    price           numeric(16,4) NOT NULL CHECK (price >= 0),
    coefficient     numeric(8,4) NOT NULL DEFAULT 1,
    section         text,
    is_commercial   boolean NOT NULL DEFAULT false,
    UNIQUE (estimate_id, line_no)
);
CREATE INDEX ix_estimate_item_est ON estimate_item(estimate_id);
CREATE INDEX ix_estimate_item_name_trgm ON estimate_item USING gin (name gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- 4. Факт: журналы работ, суточные отчёты
-- ---------------------------------------------------------------------------

CREATE TABLE journal_entry (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    work_type_id    uuid NOT NULL REFERENCES work_type(id),
    estimate_item_id uuid REFERENCES estimate_item(id),      -- сопоставление
    entry_date      date NOT NULL,
    capture_zone    text,                                    -- захватка/ось/этаж
    qty             numeric(16,4) NOT NULL CHECK (qty > 0),
    unit_code       text NOT NULL REFERENCES unit(code),
    executor_org_id uuid REFERENCES organization(id),
    source_doc_id   uuid REFERENCES file_blob(id),
    source_page     int,
    extraction_conf numeric(4,3) CHECK (extraction_conf BETWEEN 0 AND 1),
    created_by      uuid REFERENCES app_user(id),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_journal_entry_obj_date ON journal_entry(object_id, entry_date);
CREATE INDEX ix_journal_entry_item ON journal_entry(estimate_item_id);

-- ---------------------------------------------------------------------------
-- 5. Акты КС-2 / КС-3
-- ---------------------------------------------------------------------------

CREATE TABLE act_ks2 (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    contract_id     uuid NOT NULL REFERENCES contract(id),
    executor_org_id uuid NOT NULL REFERENCES organization(id),   -- субподрядчик
    number          text,
    period_from     date NOT NULL,
    period_to       date NOT NULL,
    prev_act_id     uuid REFERENCES act_ks2(id),                 -- накопительный учёт
    status          text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','ai_generated','on_review','approved','signed','rejected')),
    total_no_vat    numeric(18,2) NOT NULL DEFAULT 0,
    vat_amount      numeric(18,2) NOT NULL DEFAULT 0,
    total_with_vat  numeric(18,2) GENERATED ALWAYS AS (total_no_vat + vat_amount) STORED,
    doc_number      text,
    created_by      uuid REFERENCES app_user(id),
    approved_by     uuid REFERENCES app_user(id),
    approved_at     timestamptz,
    CHECK (period_to >= period_from)
);
CREATE INDEX ix_act_ks2_obj_period ON act_ks2(object_id, period_to);

CREATE TABLE act_ks2_item (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    act_id          uuid NOT NULL REFERENCES act_ks2(id) ON DELETE CASCADE,
    line_no         int NOT NULL,
    estimate_item_id uuid REFERENCES estimate_item(id),
    name            text NOT NULL,
    unit_code       text NOT NULL REFERENCES unit(code),
    qty             numeric(16,4) NOT NULL CHECK (qty >= 0),
    price           numeric(16,4) NOT NULL CHECK (price >= 0),
    amount          numeric(18,2) NOT NULL CHECK (amount >= 0),
    has_backing_docs boolean NOT NULL DEFAULT false,
    flags           text[] NOT NULL DEFAULT '{}',   -- VOLUME_OVER_ESTIMATE, NOT_IN_ESTIMATE, ...
    sources         jsonb NOT NULL DEFAULT '[]',    -- [{doc, page, row}]
    UNIQUE (act_id, line_no)
);
CREATE INDEX ix_ks2_item_act ON act_ks2_item(act_id);

CREATE TABLE act_ks3 (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    contract_id     uuid NOT NULL REFERENCES contract(id),
    number          text,
    period_from     date NOT NULL,
    period_to       date NOT NULL,
    status          text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','on_review','signed','rejected')),
    total_since_start numeric(18,2) NOT NULL DEFAULT 0,
    total_current_period numeric(18,2) NOT NULL DEFAULT 0,
    vat_amount      numeric(18,2) NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE act_ks3_ks2 (
    ks3_id          uuid NOT NULL REFERENCES act_ks3(id) ON DELETE CASCADE,
    ks2_id          uuid NOT NULL REFERENCES act_ks2(id),
    PRIMARY KEY (ks3_id, ks2_id)
);

-- Расхождения: результат сверки, с решением человека
CREATE TABLE discrepancy (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    act_id          uuid REFERENCES act_ks2(id),
    estimate_item_id uuid REFERENCES estimate_item(id),
    type            text NOT NULL CHECK (type IN (
                        'VOLUME_OVER_ESTIMATE','NOT_IN_ESTIMATE','PRICE_MISMATCH',
                        'UNIT_MISMATCH','DUPLICATE','MISSING_BACKING_DOCS',
                        'ALREADY_CLOSED','NO_SCHEME')),
    severity        text NOT NULL CHECK (severity IN ('critical','major','info')),
    values_json     jsonb NOT NULL,              -- {estimate, actual, delta, delta_pct}
    sources         jsonb NOT NULL,              -- ссылки на документы/листы/строки
    suggested_action text,
    human_decision  text CHECK (human_decision IN ('accepted','rejected','postponed')),
    decided_by      uuid REFERENCES app_user(id),
    decided_at      timestamptz,
    comment         text,
    detected_at     timestamptz NOT NULL DEFAULT now(),
    agent_task_id   uuid                          -- FK добавляется после agent_task
);
CREATE INDEX ix_discrepancy_obj ON discrepancy(object_id, severity);

-- ---------------------------------------------------------------------------
-- 6. Документы ИД и связи
-- ---------------------------------------------------------------------------

CREATE TABLE doc_type (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code            text NOT NULL UNIQUE,        -- AOSR, SCHEME, CERT, PROTOCOL, JOURNAL...
    name            text NOT NULL,
    is_registerable boolean NOT NULL DEFAULT true,
    template_id     text
);

CREATE TABLE document (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    doc_type_id     uuid NOT NULL REFERENCES doc_type(id),
    work_type_id    uuid REFERENCES work_type(id),
    number          text,
    doc_date        date,
    rd_cipher       text,                        -- шифр рабочей документации
    capture_zone    text,
    status          text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','on_review','approved','signed','transferred','rejected')),
    current_version_id uuid,                     -- FK ниже
    aosr_next_works text,                        -- «разрешается производство следующих работ»
    created_by      uuid REFERENCES app_user(id),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_document_obj_type ON document(object_id, doc_type_id);

CREATE TABLE document_version (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     uuid NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    version_no      int NOT NULL CHECK (version_no > 0),
    file_id         uuid NOT NULL REFERENCES file_blob(id),
    pages           int,
    is_signed       boolean NOT NULL DEFAULT false,
    signed_at       timestamptz,
    signature_ref   text,                        -- ссылка на УКЭП/ЭДО
    generated_by_ai boolean NOT NULL DEFAULT false,
    prompt_version  text,
    model           text,
    created_by      uuid REFERENCES app_user(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, version_no)
);

ALTER TABLE document
    ADD CONSTRAINT fk_document_current_version
    FOREIGN KEY (current_version_id) REFERENCES document_version(id);

-- Связи: АОСР ↔ схема ↔ сертификат ↔ позиция акта
CREATE TABLE document_link (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_document_id uuid NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    to_document_id  uuid NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    link_type       text NOT NULL CHECK (link_type IN ('appendix','basis','supersedes','related')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (from_document_id, to_document_id, link_type),
    CHECK (from_document_id <> to_document_id)
);

CREATE TABLE document_ks2_item (
    document_id     uuid NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    ks2_item_id     uuid NOT NULL REFERENCES act_ks2_item(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, ks2_item_id)
);

-- Реестр ИД (передача пакета заказчику)
CREATE TABLE id_register (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    period_from     date,
    period_to       date,
    transfer_number text,
    transferred_at  timestamptz,
    recipient_org_id uuid REFERENCES organization(id),
    status          text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','transferred','accepted','returned')),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE id_register_row (
    register_id     uuid NOT NULL REFERENCES id_register(id) ON DELETE CASCADE,
    row_no          int NOT NULL,
    document_id     uuid NOT NULL REFERENCES document(id),
    sheets          int,
    ks2_id          uuid REFERENCES act_ks2(id),
    remark          text,
    PRIMARY KEY (register_id, row_no)
);

-- ---------------------------------------------------------------------------
-- 7. Комплектность ИД (профили требований заказчиков)
-- ---------------------------------------------------------------------------

CREATE TABLE completeness_profile (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_org_id uuid REFERENCES organization(id),
    object_id       uuid REFERENCES construction_object(id),
    version         text NOT NULL,
    is_active       boolean NOT NULL DEFAULT true,
    notes           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (customer_org_id, object_id, version)
);

CREATE TABLE completeness_requirement (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_id      uuid NOT NULL REFERENCES completeness_profile(id) ON DELETE CASCADE,
    work_type_id    uuid NOT NULL REFERENCES work_type(id),
    doc_type_id     uuid NOT NULL REFERENCES doc_type(id),
    is_mandatory    boolean NOT NULL DEFAULT true,
    condition_json  jsonb,                       -- доп. условия (например, «только при сварке»)
    norm_ref        text,                        -- пункт СП/РД, обосновывающий требование
    UNIQUE (profile_id, work_type_id, doc_type_id)
);

CREATE TABLE completeness_report (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    profile_id      uuid NOT NULL REFERENCES completeness_profile(id),
    work_type_id    uuid NOT NULL REFERENCES work_type(id),
    required_count  int NOT NULL,
    present_count   int NOT NULL,
    with_issues_count int NOT NULL,
    missing_json    jsonb NOT NULL DEFAULT '[]',
    blockers_json   jsonb NOT NULL DEFAULT '[]',
    ready_for_transfer boolean NOT NULL DEFAULT false,
    agent_task_id   uuid,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Входной контроль материалов
CREATE TABLE material_certificate (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    material_name   text NOT NULL,
    mark            text,
    batch_no        text,
    doc_kind        text CHECK (doc_kind IN ('certificate','declaration','passport','protocol')),
    doc_number      text,
    issued_at       date,
    valid_to        date,
    issuer          text,
    file_id         uuid REFERENCES file_blob(id),
    is_mandatory    boolean,                     -- требуется обязательная сертификация
    status          text NOT NULL DEFAULT 'valid'
                    CHECK (status IN ('valid','expired','missing','rejected')),
    extraction_conf numeric(4,3),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_material_cert_obj ON material_certificate(object_id, material_name);

-- ---------------------------------------------------------------------------
-- 8. Нормативная база с версионированием (RAG)
-- ---------------------------------------------------------------------------

CREATE TABLE norm_document (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL CHECK (kind IN ('СП','ГОСТ','СНиП','ТР ТС','СанПиН','ФЕР','ТЕР','ГЭСН','local')),
    designation     text NOT NULL,               -- «СП 63.13330.2018»
    title           text NOT NULL,
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','superseded','repealed','draft')),
    superseded_by_id uuid REFERENCES norm_document(id),
    issuer          text,
    UNIQUE (kind, designation)
);

CREATE TABLE norm_revision (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    norm_document_id uuid NOT NULL REFERENCES norm_document(id) ON DELETE CASCADE,
    edition_label   text NOT NULL,               -- «с изм. 1, 2»
    effective_from  date NOT NULL,
    effective_to    date,
    status          text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','superseded','repealed')),
    file_id         uuid REFERENCES file_blob(id),
    content_hash    char(64),
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    UNIQUE (norm_document_id, edition_label)
);
CREATE INDEX ix_norm_revision_date ON norm_revision(effective_from, effective_to);

CREATE TABLE norm_chunk (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    revision_id     uuid NOT NULL REFERENCES norm_revision(id) ON DELETE CASCADE,
    clause          text NOT NULL,               -- «10.3.2»
    clause_title    text,
    body            text NOT NULL,               -- полный текст пункта, без обрезки
    page_from       int,
    page_to         int,
    order_no        int NOT NULL,
    embedding       vector(1024),                -- BGE-M3
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('russian', body)) STORED,
    UNIQUE (revision_id, clause)
);
CREATE INDEX ix_norm_chunk_tsv ON norm_chunk USING gin (tsv);
CREATE INDEX ix_norm_chunk_emb ON norm_chunk USING hnsw (embedding vector_cosine_ops);

-- Журнал нормативных запросов (для аудита и контроля галлюцинаций)
CREATE TABLE norm_query_log (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid REFERENCES app_user(id),
    object_id       uuid REFERENCES construction_object(id),
    query_text      text NOT NULL,
    as_of_date      date NOT NULL,               -- дата применимости норм
    retrieved_ids   uuid[] NOT NULL DEFAULT '{}',
    answer_type     text CHECK (answer_type IN ('answer','not_found','ambiguous')),
    confidence      text CHECK (confidence IN ('high','medium','low')),
    citations       jsonb,
    cite_verified   boolean,                     -- результат cite_verify
    agent_task_id   uuid,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 9. CAD-модуль: чертежи, версии, операции, выпуск схем
-- ---------------------------------------------------------------------------

CREATE TABLE cad_template (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL,               -- «corp-A/v4»
    layers_json     jsonb NOT NULL,              -- допустимые слои и их назначение
    titleblock_json jsonb NOT NULL,              -- поля штампа
    allowed_ops     text[] NOT NULL,
    is_active       boolean NOT NULL DEFAULT true
);

CREATE TABLE drawing (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_id       uuid NOT NULL REFERENCES construction_object(id),
    work_type_id    uuid REFERENCES work_type(id),
    rd_cipher       text,
    units           text NOT NULL DEFAULT 'mm',
    scale           text,                        -- «1:100», подтверждается человеком
    scale_confirmed_by uuid REFERENCES app_user(id),
    template_id     uuid REFERENCES cad_template(id),
    current_version_id uuid,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE drawing_version (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    drawing_id      uuid NOT NULL REFERENCES drawing(id) ON DELETE CASCADE,
    version_no      int NOT NULL CHECK (version_no > 0),
    file_id         uuid NOT NULL REFERENCES file_blob(id),
    file_format     text NOT NULL CHECK (file_format IN ('dwg','dxf')),
    entity_count    int,                         -- контроль потерь при конвертации
    source          text NOT NULL CHECK (source IN ('project','redline','aosr','manual')),
    parent_version_id uuid REFERENCES drawing_version(id),
    created_by      uuid REFERENCES app_user(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (drawing_id, version_no)
);
ALTER TABLE drawing
    ADD CONSTRAINT fk_drawing_current_version
    FOREIGN KEY (current_version_id) REFERENCES drawing_version(id);

CREATE TABLE cad_op_set (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    drawing_id      uuid NOT NULL REFERENCES drawing(id),
    source_type     text NOT NULL CHECK (source_type IN ('redline','text','aosr','bim')),
    source_file_id  uuid REFERENCES file_blob(id),
    aosr_document_id uuid REFERENCES document(id),
    status          text NOT NULL DEFAULT 'planned'
                    CHECK (status IN ('planned','validated','confirmed','applied','rejected')),
    created_by      uuid REFERENCES app_user(id),
    confirmed_by    uuid REFERENCES app_user(id),
    confirmed_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cad_operation (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    op_set_id       uuid NOT NULL REFERENCES cad_op_set(id) ON DELETE CASCADE,
    op_no           int NOT NULL,
    op_type         text NOT NULL,               -- ADD_POLYLINE, MOVE_ENTITY, ...
    params          jsonb NOT NULL,
    reason          text,
    source          jsonb,                       -- {type, ref, confidence}
    confidence      numeric(4,3),
    requires_confirmation boolean NOT NULL DEFAULT true,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','applied','rejected','needs_human_decision')),
    applied_at      timestamptz,
    UNIQUE (op_set_id, op_no)
);

CREATE TABLE scheme_export (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    drawing_id      uuid NOT NULL REFERENCES drawing(id),
    drawing_version_id uuid NOT NULL REFERENCES drawing_version(id),
    outputs         text[] NOT NULL,             -- {pdf, dwg, dxf}
    titleblock_json jsonb NOT NULL,
    linked_aosr     uuid[] NOT NULL DEFAULT '{}',
    document_id     uuid REFERENCES document(id),-- запись в реестре ИД
    validation_json jsonb NOT NULL,              -- результаты валидаторов
    exported_by     uuid NOT NULL REFERENCES app_user(id),
    exported_at     timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 10. Задачи агентов и шаги (трассировка)
-- ---------------------------------------------------------------------------

CREATE TABLE agent_task (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid REFERENCES app_user(id),
    object_id       uuid REFERENCES construction_object(id),
    agent           text NOT NULL CHECK (agent IN (
                        'orchestrator','documenter','estimator','normative',
                        'scheduler','cad','qc')),
    intent          text,
    request_text    text NOT NULL,
    plan_json       jsonb,
    status          text NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','awaiting_human','done','failed','cancelled')),
    result_json     jsonb,
    model           text,
    prompt_version  text,
    tokens_in       int,
    tokens_out      int,
    cost_rub        numeric(12,4),
    latency_ms      int,
    quality_score   numeric(4,3),                -- оценка «Контролёра»
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX ix_agent_task_obj ON agent_task(object_id, created_at DESC);

CREATE TABLE agent_step (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         uuid NOT NULL REFERENCES agent_task(id) ON DELETE CASCADE,
    step_no         int NOT NULL,
    agent           text NOT NULL,
    tool            text,
    args_json       jsonb,
    result_summary  text,
    ok              boolean NOT NULL DEFAULT true,
    error_text      text,
    duration_ms     int,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, step_no)
);

ALTER TABLE discrepancy
    ADD CONSTRAINT fk_discrepancy_task FOREIGN KEY (agent_task_id) REFERENCES agent_task(id);
ALTER TABLE completeness_report
    ADD CONSTRAINT fk_completeness_task FOREIGN KEY (agent_task_id) REFERENCES agent_task(id);
ALTER TABLE norm_query_log
    ADD CONSTRAINT fk_norm_query_task FOREIGN KEY (agent_task_id) REFERENCES agent_task(id);

-- ---------------------------------------------------------------------------
-- 11. Журнал аудита с хэш-цепочкой (append-only)
-- ---------------------------------------------------------------------------

CREATE TABLE audit_log (
    id              bigserial PRIMARY KEY,
    ts              timestamptz NOT NULL DEFAULT now(),
    actor_type      text NOT NULL CHECK (actor_type IN ('user','agent','system')),
    actor_id        text,                        -- user_id или agent name
    action          text NOT NULL,               -- document.sign, cad.apply_commit, ks2.approve...
    entity_type     text NOT NULL,
    entity_id       uuid,
    object_id       uuid REFERENCES construction_object(id),
    payload_json    jsonb,
    prev_hash       char(64),
    hash            char(64) NOT NULL
);

-- Запрет изменения и удаления записей аудита
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_log_immutable
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

-- Хэш-цепочка: hash = sha256(prev_hash || ts || actor || action || entity || payload)
CREATE OR REPLACE FUNCTION audit_log_chain() RETURNS trigger AS $$
DECLARE
    last_hash char(64);
BEGIN
    SELECT hash INTO last_hash FROM audit_log ORDER BY id DESC LIMIT 1;
    NEW.prev_hash := COALESCE(last_hash, repeat('0', 64));
    NEW.hash := encode(digest(
        NEW.prev_hash || NEW.ts::text || COALESCE(NEW.actor_type,'') ||
        COALESCE(NEW.actor_id,'') || NEW.action || NEW.entity_type ||
        COALESCE(NEW.entity_id::text,'') || COALESCE(NEW.payload_json::text,''),
        'sha256'), 'hex');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_log_chain
    BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_chain();

CREATE INDEX ix_audit_log_entity ON audit_log(entity_type, entity_id);
CREATE INDEX ix_audit_log_ts ON audit_log(ts DESC);

-- ---------------------------------------------------------------------------
-- 12. Изоляция доступа (RLS) — пример для документов
--     Роль приложения устанавливает: SET LOCAL app.organization_id = '…';
-- ---------------------------------------------------------------------------

ALTER TABLE document ENABLE ROW LEVEL SECURITY;
ALTER TABLE act_ks2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE drawing ENABLE ROW LEVEL SECURITY;

CREATE POLICY p_document_org ON document
    USING (object_id IN (
        SELECT id FROM construction_object
        WHERE organization_id = current_setting('app.organization_id', true)::uuid
    ));

CREATE POLICY p_act_ks2_org ON act_ks2
    USING (object_id IN (
        SELECT id FROM construction_object
        WHERE organization_id = current_setting('app.organization_id', true)::uuid
    ));

CREATE POLICY p_drawing_org ON drawing
    USING (object_id IN (
        SELECT id FROM construction_object
        WHERE organization_id = current_setting('app.organization_id', true)::uuid
    ));

-- ---------------------------------------------------------------------------
-- 13. Представления для агентов и отчётов
-- ---------------------------------------------------------------------------

-- Позиции, готовые к включению в КС-2 (есть факт и подтверждающая ИД)
CREATE OR REPLACE VIEW v_ks2_ready_positions AS
SELECT e.object_id,
       ei.id                AS estimate_item_id,
       ei.code,
       ei.name,
       ei.unit_code,
       ei.qty               AS qty_estimate,
       COALESCE(SUM(j.qty), 0) AS qty_actual,
       ei.price,
       COALESCE(SUM(j.qty), 0) * ei.price AS amount,
       EXISTS (SELECT 1 FROM document d
               WHERE d.object_id = e.object_id
                 AND d.status IN ('approved','signed')) AS has_backing_docs
FROM estimate_item ei
JOIN estimate e ON e.id = ei.estimate_id AND e.status = 'approved'
LEFT JOIN journal_entry j ON j.estimate_item_id = ei.id
GROUP BY e.object_id, ei.id, ei.code, ei.name, ei.unit_code, ei.qty, ei.price;

-- Сводка комплектности по объекту
CREATE OR REPLACE VIEW v_completeness_summary AS
SELECT object_id,
       COUNT(*) FILTER (WHERE ready_for_transfer)     AS work_types_ready,
       COUNT(*) FILTER (WHERE NOT ready_for_transfer) AS work_types_blocked,
       SUM(required_count)  AS docs_required,
       SUM(present_count)   AS docs_present,
       ROUND(100.0 * SUM(present_count) / NULLIF(SUM(required_count),0), 1) AS completeness_pct
FROM completeness_report
GROUP BY object_id;

-- Актуальная редакция нормы на заданную дату
CREATE OR REPLACE VIEW v_norm_actual AS
SELECT nd.id AS norm_document_id,
       nd.kind,
       nd.designation,
       nr.id AS revision_id,
       nr.edition_label,
       nr.effective_from
FROM norm_document nd
JOIN norm_revision nr ON nr.norm_document_id = nd.id
WHERE nr.status = 'active'
  AND nr.effective_from <= CURRENT_DATE
  AND (nr.effective_to IS NULL OR nr.effective_to >= CURRENT_DATE);

COMMENT ON TABLE audit_log IS 'Неизменяемый журнал действий: append-only + хэш-цепочка, срок хранения >= 3 лет';
COMMENT ON TABLE norm_revision IS 'Версия редакции нормативного документа: основа корректности ответов с датой применимости';
COMMENT ON COLUMN cad_operation.confidence IS 'Уверенность распознавания пометки; < 0.7 — только в needs_human_decision';
