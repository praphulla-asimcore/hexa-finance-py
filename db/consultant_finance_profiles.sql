-- Per-consultant finance profile pulled from HexaFlow after a CSI ingest
-- (APEX -> HexaFlow Pack 5: GET .../csi/runs/<run_id>/consultant-finance-profiles,
-- purpose=finance_payment_review). Unmasked bank/statutory/ID/salary data — same
-- PII sensitivity as consultant_documents; never log these column values.
--
-- One row per (case_id, employee_id): the roster is tied to the run_id the
-- owning case was ingested with (payroll_cases.parsed_data.hexaflow_csi_run_id)
-- and is never replaced by a later/different run's roster — a new HexaFlow run
-- always opens a NEW payroll_cases row (duplicate run_ref is rejected at
-- ingest), so this table naturally never mixes rosters across runs.
-- Run against the SAME database the app uses (DATABASE_URL), like the other db/*.sql.

create table if not exists consultant_finance_profiles (
    id                    uuid primary key default gen_random_uuid(),
    case_id               uuid not null references payroll_cases (id),
    hexaflow_run_id       varchar(100) not null,
    employee_id           varchar(50)  not null,
    id_type               varchar(50),
    id_number             text,
    bank_name             text,
    bank_account_number   text,
    bank_code             varchar(10),
    epf_number            text,
    socso_number          text,
    eis_number            text,
    tin_number            text,
    salary_data           jsonb,
    pulled_at             timestamptz  not null default now(),

    constraint consultant_finance_profiles_unique
        unique (case_id, employee_id)
);

comment on table consultant_finance_profiles is
    'Unmasked consultant finance data (bank/statutory/ID/salary) pulled from '
    'HexaFlow for finance_payment_review purposes. PII -- never log column values.';
