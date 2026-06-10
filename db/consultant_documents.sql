-- Consultant supporting documents (timesheets, POs, WCNs, contracts).
-- File content is stored INLINE as base64 in file_data — the same pattern as
-- payroll_cases.bank_file_data / statutory_submissions.submission_file — because
-- this app has no disk/object storage; everything round-trips through DB columns.
-- file_url keeps the original external CSI-Generator link for reference only; it
-- is NOT the source of truth (the bytes in file_data are). file_hash is the
-- SHA-256 of the stored bytes, with hash_verified flipped true once re-checked.
-- Run against the SAME database the app uses (DATABASE_URL), like the other db/*.sql.

create table if not exists consultant_documents (
    id                  uuid primary key default gen_random_uuid(),
    -- FK to payroll_cases (UUID id). Nullable: a document can exist before it is
    -- attached to a specific case (e.g. manual upload pending a case).
    case_id             uuid references payroll_cases (id),
    consultant_id       varchar(50)  not null,
    consultant_name     varchar(200) not null,
    entity              varchar(20)  not null,
    period_month        varchar(7)   not null,            -- format YYYY-MM
    document_type       varchar(20)  not null,            -- TIMESHEET | PO | WCN | CONTRACT
    filename            varchar(500) not null,
    file_url            text,                              -- original external URL (reference only)
    file_data           text,                              -- base64-encoded file content (actual storage)
    file_hash           varchar(64)  not null,            -- SHA-256 hex
    hash_verified       boolean      not null default false,
    source              varchar(20)  not null,            -- CSI_GENERATOR | MANUAL_UPLOAD
    client_signed       boolean      not null default false,
    signed_by           varchar(200),
    signed_at           date,
    valid_from          date,
    valid_to            date,
    po_value            numeric(15, 2),
    po_currency         varchar(5),
    fe_sighted          boolean      not null default false,
    fe_sighted_by       integer,
    fe_sighted_at       timestamptz,
    fe_checklist        jsonb,
    fe_rejection_reason text,
    uploaded_at         timestamptz  not null default now(),
    uploaded_by         varchar(200),

    -- One document of a given type per consultant, per case, per period month.
    -- NOTE: case_id is nullable, and Postgres treats NULLs as distinct, so this
    -- does NOT dedupe rows whose case_id is NULL — only case-attached documents.
    constraint consultant_documents_unique_doc
        unique (case_id, consultant_id, document_type, period_month)
);
