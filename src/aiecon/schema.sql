-- aiecon DuckDB schema (PLAN.md section 4.5). Five core tables hold the data model; the
-- meta_* tables are bookkeeping needed for idempotent replay, snapshot activation and
-- run selection. Every core row also keeps its full JSON document (doc_json) so the
-- Pydantic projection can be rebuilt exactly.

CREATE TABLE IF NOT EXISTS meta_workspace (
    key                 VARCHAR PRIMARY KEY,
    value               VARCHAR NOT NULL
);

-- processed raw events: duplicate / conflict detection on replay
CREATE TABLE IF NOT EXISTS meta_events (
    dataset_id          VARCHAR NOT NULL,
    event_id            VARCHAR NOT NULL,
    content_hash        VARCHAR NOT NULL,
    event_type          VARCHAR NOT NULL,
    revision            INTEGER NOT NULL,
    target_id           VARCHAR NOT NULL,      -- call_id or workflow_run_id
    source_file         VARCHAR,
    source_line         INTEGER,
    processed_at_ms     BIGINT NOT NULL,
    applied             BOOLEAN NOT NULL,       -- false = recorded but superseded by higher revision
    PRIMARY KEY (dataset_id, event_id)
);

-- ingest file registry: skip unchanged files, record damage without content
CREATE TABLE IF NOT EXISTS meta_ingest_files (
    file_path           VARCHAR NOT NULL,
    file_sha256         VARCHAR NOT NULL,
    dataset_id          VARCHAR,
    line_count          INTEGER NOT NULL,
    accepted            INTEGER NOT NULL,
    duplicates          INTEGER NOT NULL,
    conflicts           INTEGER NOT NULL,
    rejected            INTEGER NOT NULL,
    truncated_tail      BOOLEAN NOT NULL,
    processed_at_ms     BIGINT NOT NULL,
    PRIMARY KEY (file_path, file_sha256)
);

-- provider snapshots: exactly one active snapshot per (provider, scope, kind, grain, window)
CREATE TABLE IF NOT EXISTS meta_snapshots (
    snapshot_id         VARCHAR PRIMARY KEY,
    provider            VARCHAR NOT NULL,
    scope_id            VARCHAR NOT NULL,
    record_kind         VARCHAR NOT NULL,
    data_kind           VARCHAR NOT NULL,
    grain               VARCHAR NOT NULL,
    window_start_ms     BIGINT NOT NULL,
    window_end_ms       BIGINT NOT NULL,
    fetched_at_ms       BIGINT NOT NULL,
    source_hash         VARCHAR NOT NULL,
    finality            VARCHAR NOT NULL,
    snapshot_complete   BOOLEAN NOT NULL,
    active              BOOLEAN NOT NULL,
    record_count        INTEGER NOT NULL,
    manifest_json       VARCHAR NOT NULL
);

-- pricing / reconcile runs and which one the report selects
CREATE TABLE IF NOT EXISTS meta_runs (
    run_id              VARCHAR PRIMARY KEY,
    run_kind            VARCHAR NOT NULL,       -- pricing | reconcile
    dataset_id          VARCHAR NOT NULL,
    created_at_ms       BIGINT NOT NULL,
    active              BOOLEAN NOT NULL,
    manifest_json       VARCHAR NOT NULL
);

-- ------------------------------------------------------------------ core tables
CREATE TABLE IF NOT EXISTS calls (
    dataset_id                  VARCHAR NOT NULL,
    call_id                     VARCHAR NOT NULL,
    data_kind                   VARCHAR NOT NULL,
    scope_id                    VARCHAR NOT NULL,
    workflow_id                 VARCHAR,
    workflow_run_id             VARCHAR,
    node_id                     VARCHAR,
    node_run_id                 VARCHAR,
    attempt_index               INTEGER,
    retry_of_call_id            VARCHAR,
    fallback_of_call_id         VARCHAR,
    provider                    VARCHAR NOT NULL,
    api_family                  VARCHAR NOT NULL,
    model_requested             VARCHAR NOT NULL,
    model_resolved              VARCHAR,
    service_tier                VARCHAR,
    inference_region            VARCHAR,
    provider_request_id         VARCHAR,
    started_at_ms               BIGINT,
    ended_at_ms                 BIGINT,
    status                      VARCHAR NOT NULL,
    error_class                 VARCHAR,
    usage_format                VARCHAR,
    input_total_tokens          BIGINT,
    input_uncached_tokens       BIGINT,
    input_cache_read_tokens     BIGINT,
    input_cache_write_tokens    BIGINT,
    output_tokens               BIGINT,
    usage_completeness          VARCHAR NOT NULL,
    upstream_cost_estimate_usd  DECIMAL(24,12),
    prefix_fingerprint          VARCHAR,
    fingerprint_key_id          VARCHAR,
    prefix_tokens               BIGINT,
    cache_policy                VARCHAR,
    stream                      BOOLEAN,
    revision                    INTEGER NOT NULL,
    normalizer_version          VARCHAR NOT NULL,
    doc_json                    VARCHAR NOT NULL,
    PRIMARY KEY (dataset_id, call_id)
);

CREATE TABLE IF NOT EXISTS outcomes (
    dataset_id          VARCHAR NOT NULL,
    workflow_run_id     VARCHAR NOT NULL,
    workflow_id         VARCHAR,
    data_kind           VARCHAR NOT NULL,
    revision            INTEGER NOT NULL,
    terminal_at_ms      BIGINT,
    status              VARCHAR NOT NULL,
    success             BOOLEAN,
    outcome_source      VARCHAR NOT NULL,
    doc_json            VARCHAR NOT NULL,
    PRIMARY KEY (dataset_id, workflow_run_id)
);

CREATE TABLE IF NOT EXISTS provider_records (
    snapshot_id         VARCHAR NOT NULL,
    record_id           VARCHAR NOT NULL,
    provider            VARCHAR NOT NULL,
    scope_id            VARCHAR NOT NULL,
    record_kind         VARCHAR NOT NULL,
    data_kind           VARCHAR NOT NULL,
    window_start_ms     BIGINT NOT NULL,
    window_end_ms       BIGINT NOT NULL,
    grain               VARCHAR NOT NULL,
    dimensions_json     VARCHAR NOT NULL,
    dim_model           VARCHAR,               -- convenience copies of common dimensions
    dim_project         VARCHAR,
    dim_line_item       VARCHAR,
    amount_original     DECIMAL(24,12),
    amount_unit         VARCHAR,
    currency            VARCHAR,
    amount_usd          DECIMAL(24,12),
    usage_json          VARCHAR,
    source_ref          VARCHAR NOT NULL,
    source_hash         VARCHAR NOT NULL,
    fetched_at_ms       BIGINT NOT NULL,
    finality            VARCHAR NOT NULL,
    snapshot_complete   BOOLEAN NOT NULL,
    doc_json            VARCHAR NOT NULL,
    PRIMARY KEY (snapshot_id, record_id)
);

CREATE TABLE IF NOT EXISTS cost_line_items (
    dataset_id          VARCHAR NOT NULL,
    call_id             VARCHAR NOT NULL,
    resource            VARCHAR NOT NULL,
    pricing_run_id      VARCHAR NOT NULL,
    line_item_id        VARCHAR NOT NULL,
    data_kind           VARCHAR NOT NULL,
    quantity            BIGINT,
    unit                VARCHAR NOT NULL,
    unit_quantity       BIGINT,
    unit_price          DECIMAL(24,12),
    currency            VARCHAR,
    line_cost           DECIMAL(24,12),
    status              VARCHAR NOT NULL,
    unpriced_reason     VARCHAR,
    evidence_class      VARCHAR NOT NULL,
    catalog_version     VARCHAR,
    price_id            VARCHAR,
    boundary_call       BOOLEAN NOT NULL,
    doc_json            VARCHAR NOT NULL,
    PRIMARY KEY (dataset_id, call_id, resource, pricing_run_id)
);

CREATE TABLE IF NOT EXISTS reconciliation_buckets (
    reconcile_run_id    VARCHAR NOT NULL,
    bucket_key          VARCHAR NOT NULL,
    comparison_kind     VARCHAR NOT NULL,
    dataset_id          VARCHAR NOT NULL,
    provider            VARCHAR NOT NULL,
    scope_id            VARCHAR NOT NULL,
    window_start_ms     BIGINT NOT NULL,
    window_end_ms       BIGINT NOT NULL,
    grain               VARCHAR NOT NULL,
    dimensions_json     VARCHAR NOT NULL,
    local_estimate_usd  DECIMAL(24,12),
    provider_cost_usd   DECIMAL(24,12),
    signed_variance_usd DECIMAL(24,12),
    variance_pct        DECIMAL(24,12),
    status              VARCHAR NOT NULL,
    doc_json            VARCHAR NOT NULL,
    PRIMARY KEY (reconcile_run_id, bucket_key, comparison_kind)
);

-- ------------------------------------------------------------------------ views
CREATE OR REPLACE VIEW current_pricing_run AS
    SELECT run_id AS pricing_run_id, dataset_id, created_at_ms, manifest_json
    FROM meta_runs WHERE run_kind = 'pricing' AND active;

CREATE OR REPLACE VIEW current_cost_line_items AS
    SELECT li.*
    FROM cost_line_items li
    JOIN current_pricing_run r
      ON li.pricing_run_id = r.pricing_run_id AND li.dataset_id = r.dataset_id;

CREATE OR REPLACE VIEW current_provider_records AS
    SELECT pr.*
    FROM provider_records pr
    JOIN meta_snapshots s ON pr.snapshot_id = s.snapshot_id
    WHERE s.active AND s.snapshot_complete;

CREATE OR REPLACE VIEW call_costs AS
    SELECT
        c.dataset_id,
        c.call_id,
        c.workflow_id,
        c.workflow_run_id,
        c.node_id,
        c.node_run_id,
        c.provider,
        c.model_resolved,
        c.scope_id,
        c.status,
        c.started_at_ms,
        c.ended_at_ms,
        c.usage_completeness,
        COALESCE(SUM(li.line_cost) FILTER (WHERE li.status = 'priced'), 0) AS known_cost_usd,
        COUNT(li.line_item_id) FILTER (WHERE li.status = 'unpriced') AS unpriced_line_items,
        COUNT(li.line_item_id) AS line_items,
        (COUNT(li.line_item_id) FILTER (WHERE li.status = 'unpriced') = 0
            AND COUNT(li.line_item_id) > 0
            AND c.usage_completeness = 'complete') AS cost_complete
    FROM calls c
    LEFT JOIN current_cost_line_items li
      ON li.dataset_id = c.dataset_id AND li.call_id = c.call_id
    GROUP BY c.dataset_id, c.call_id, c.workflow_id, c.workflow_run_id, c.node_id,
             c.node_run_id, c.provider, c.model_resolved, c.scope_id, c.status,
             c.started_at_ms, c.ended_at_ms, c.usage_completeness;

CREATE OR REPLACE VIEW workflow_runs AS
    SELECT
        cc.dataset_id,
        cc.workflow_run_id,
        ANY_VALUE(cc.workflow_id) AS workflow_id,
        COUNT(*) AS call_count,
        SUM(cc.known_cost_usd) AS known_cost_usd,
        BOOL_AND(cc.cost_complete) AS cost_complete,
        MIN(cc.started_at_ms) AS first_started_at_ms,
        MAX(cc.ended_at_ms) AS last_ended_at_ms,
        o.status AS outcome_status,
        o.success AS outcome_success,
        o.terminal_at_ms
    FROM call_costs cc
    LEFT JOIN outcomes o
      ON o.dataset_id = cc.dataset_id AND o.workflow_run_id = cc.workflow_run_id
    WHERE cc.workflow_run_id IS NOT NULL
    GROUP BY cc.dataset_id, cc.workflow_run_id, o.status, o.success, o.terminal_at_ms;

CREATE OR REPLACE VIEW node_runs AS
    SELECT
        dataset_id,
        workflow_run_id,
        node_run_id,
        ANY_VALUE(node_id) AS node_id,
        COUNT(*) AS call_count,
        SUM(known_cost_usd) AS known_cost_usd,
        BOOL_AND(cost_complete) AS cost_complete
    FROM call_costs
    WHERE node_run_id IS NOT NULL
    GROUP BY dataset_id, workflow_run_id, node_run_id;
