-- Core tables: users, payroll_cases, payroll_approval_tokens, payroll_audit_log,
-- statutory_submissions, journal_posts.
--
-- These were originally created by hand in Supabase and cloned to DigitalOcean
-- without ever being committed. Reconstructed 2026-10-27 from how the code
-- reads and writes them, when the DigitalOcean database was lost. Run this
-- FIRST on a fresh database; the other db/*.sql files reference payroll_cases.
-- scripts/setup_new_database.py applies everything in the right order.
--
-- Type conventions (must match app/services/pg_shim.py):
--   * every Python dict/list is written as Jsonb  -> jsonb columns
--   * timestamps are written as ISO strings        -> timestamptz
--   * "YYYY-MM-DD" strings                          -> date
--   * month keys like "202606" / "2026-06"          -> text

create table if not exists users (
    id              uuid primary key default gen_random_uuid(),
    email           text not null unique,
    name            text,
    role            text not null default 'arranger',
    status          text not null default 'invited',   -- invited | active | ...
    password_hash   text,
    invite_token    text unique,
    invite_expires  timestamptz,
    last_login      timestamptz,
    country_scope   text,                               -- NULL = all countries
    created_at      timestamptz not null default now()
);

create table if not exists payroll_cases (
    id                          uuid primary key default gen_random_uuid(),
    reference                   text not null,
    type                        text not null,          -- CSI | PAYROLL
    entity                      text not null,
    entity_name                 text,
    period                      text not null,
    seq_no                      integer not null,
    status                      text not null,
    parsed_data                 jsonb,
    check_data                  jsonb,

    original_file_name          text,
    original_file_hash          text,
    upload_ip                   text,
    uploaded_at                 timestamptz,
    uploaded_by_id              text,
    uploaded_by_email           text,
    uploaded_by_name            text,

    check_generated_at          timestamptz,
    check_approval_sent_at      timestamptz,
    check_approval_cert         jsonb,
    check_reviewer_approved_at  timestamptz,
    check_reviewer_name         text,
    check_final_approver_name   text,
    check_approved_at           timestamptz,
    check_rejected_at           timestamptz,
    check_rejection_reason      text,

    payment_approval_sent_at    timestamptz,
    payment_approval_cert       jsonb,
    payment_approved_at         timestamptz,
    payment_approved_by         text,
    payment_rejected_at         timestamptz,
    payment_rejection_reason    text,
    payment_date                date,

    bank_file_data              text,                   -- base64
    bank_file_name              text,
    bank_file_hash              text,
    bank_file_generated_at      timestamptz,
    bank_file_triggered_by      text,
    bank_portal_ref             text,
    bank_upload_at              timestamptz,
    bank_upload_by              text,
    bank_receipt_data           text,                   -- base64
    bank_receipt_name           text,

    zoho_journal_ids            jsonb,
    zoho_org_id                 text,
    zoho_posted_at              timestamptz,
    zoho_posted_by              text,

    audit_assembled_at          timestamptz,
    invoiced                    boolean not null default false,
    created_at                  timestamptz not null default now()
);
create index if not exists idx_payroll_cases_type_entity_period
    on payroll_cases (type, entity, period);
create index if not exists idx_payroll_cases_status on payroll_cases (status);

create table if not exists payroll_approval_tokens (
    id              uuid primary key default gen_random_uuid(),
    case_id         uuid not null references payroll_cases (id) on delete cascade,
    token           text not null unique,
    step            integer not null,
    approver_email  text,
    approver_name   text,
    approver_role   text,                               -- reviewer | final | ...
    status          text not null default 'pending',
    action_at       timestamptz,
    created_at      timestamptz not null default now()
);
create index if not exists idx_approval_tokens_case on payroll_approval_tokens (case_id);

create table if not exists payroll_audit_log (
    id              uuid primary key default gen_random_uuid(),
    case_id         uuid references payroll_cases (id) on delete cascade,
    event_type      text not null,
    performed_by    text,
    user_id         text,
    ip_address      text,
    metadata        jsonb,
    created_at      timestamptz not null default now()
);
create index if not exists idx_audit_log_case on payroll_audit_log (case_id, created_at);

create table if not exists statutory_submissions (
    id                    uuid primary key default gen_random_uuid(),
    entity                text not null,
    entity_name           text,
    statutory_type        text not null,
    wage_month            text not null,
    contribution_month    text,
    due_date              date,
    status                text not null,
    case_ids              jsonb not null default '[]'::jsonb,
    employee_data         jsonb,
    total_ee_amount       numeric(14,2),
    total_er_amount       numeric(14,2),
    total_amount          numeric(14,2),
    submission_file       text,                         -- base64
    submission_file_name  text,
    created_by            text,
    confirmed_at          timestamptz,
    confirmed_by          text,
    payment_date          date,
    payment_ref           text,
    zoho_journal_id       text,
    zoho_posted_at        timestamptz,
    created_at            timestamptz not null default now()
);
create index if not exists idx_statutory_lookup
    on statutory_submissions (entity, statutory_type, wage_month);

create table if not exists journal_posts (
    id                uuid primary key default gen_random_uuid(),
    module            text not null,
    entity            text,
    org_id            text,
    journal_id        text,
    journal_date      date,
    reference_number  text,
    total_amount      numeric(14,2),
    notes             text,
    posted_by_email   text,
    posted_by_name    text,
    posted_at         timestamptz not null default now()
);
create index if not exists idx_journal_posts_ref on journal_posts (reference_number);
