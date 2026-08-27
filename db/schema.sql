-- Audit trail for every order this service attempts.
--
-- The point of these four tables is to answer, after the fact, why a particular
-- email did or did not become a sales order: what the model read out of it, what
-- those names resolved to, what was sent to SAP, and what came back. That question
-- comes up the morning after, when the mailbox is empty and the customer is asking.
--
--   psql "$DATABASE_URL" -f db/schema.sql
--
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS transactions (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_type TEXT        NOT NULL,
    current_state    TEXT        NOT NULL,
    source           TEXT        NOT NULL,
    external_ref     TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT transactions_state_known CHECK (current_state IN (
        'STARTED', 'EXTRACTED', 'RESOLVED', 'SUBMITTED', 'COMPLETED', 'FAILED'
    ))
);

-- The mail poller looks up "have I already handled this message?" on every pass,
-- and the same Gmail id must never produce two sales orders.
CREATE UNIQUE INDEX IF NOT EXISTS transactions_source_ref_key
    ON transactions (source, external_ref)
    WHERE external_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS transactions_state_created_idx
    ON transactions (current_state, created_at DESC);


-- One row per model call: what went in, what came out, how long it took. Kept
-- separately from the transaction because a single order may take several passes.
CREATE TABLE IF NOT EXISTS agent_runs (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id BIGINT      NOT NULL REFERENCES transactions (id) ON DELETE CASCADE,
    agent_name     TEXT        NOT NULL,
    status         TEXT        NOT NULL,
    input_data     JSONB,
    output_data    JSONB,
    duration_ms    INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_runs_transaction_idx
    ON agent_runs (transaction_id, created_at);


-- One row per call into the ERP. request_data/response_data hold the BAPI payload
-- and its RETURN table verbatim, which is the only way to explain afterwards why
-- SAP rejected something.
CREATE TABLE IF NOT EXISTS erp_actions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id BIGINT      NOT NULL REFERENCES transactions (id) ON DELETE CASCADE,
    erp_system     TEXT        NOT NULL DEFAULT 'SAP',
    action_type    TEXT        NOT NULL,
    status         TEXT        NOT NULL,
    document_id    TEXT,
    request_data   JSONB,
    response_data  JSONB,
    duration_ms    INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS erp_actions_transaction_idx
    ON erp_actions (transaction_id, created_at);

CREATE INDEX IF NOT EXISTS erp_actions_document_idx
    ON erp_actions (document_id)
    WHERE document_id IS NOT NULL;


-- Failures, with the traceback. transaction_id is nullable because the poller can
-- fail before it has an order to attach the failure to.
CREATE TABLE IF NOT EXISTS system_errors (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id BIGINT      REFERENCES transactions (id) ON DELETE CASCADE,
    service_name   TEXT        NOT NULL,
    error_type     TEXT,
    error_message  TEXT        NOT NULL,
    traceback      TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS system_errors_transaction_idx
    ON system_errors (transaction_id, created_at);

CREATE INDEX IF NOT EXISTS system_errors_created_idx
    ON system_errors (created_at DESC);
