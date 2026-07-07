-- Standing, cross-run consultant master sourced from HexaFlow's Consultant
-- Finance Profile pull (app/services/hexaflow_finance_profiles.py). Unlike
-- consultant_finance_profiles (an immutable per-run audit trail keyed by
-- case_id + employee_id), this table is deliberately a LIVE directory: every
-- successful pull upserts here keyed by employee_id alone, so the latest run
-- always wins regardless of which case it came from.
--
-- app/services/bank_files.py merges this in alongside Airtable and
-- consultant_bank_overrides (fetch_hexaflow_directory / build_consultant_list),
-- so it backs bank-file generation for EVERY case -- HexaFlow-ingested or
-- manually uploaded -- not just the run it was pulled from. Precedence when
-- sources disagree: consultant_bank_overrides (manual) > this table > Airtable.
--
-- Never carries a Favourite Beneficiary Code -- HexaFlow doesn't originate
-- that; it's a Maybank CMS artifact assigned only after manual registration.
-- Unmasked bank/statutory/ID/salary data -- same PII sensitivity as
-- consultant_documents; never log these column values.
-- Run against the SAME database the app uses (DATABASE_URL), like the other db/*.sql.

create table if not exists consultant_directory (
    employee_id           varchar(50) primary key,
    consultant_name       varchar(200),
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
    source                varchar(20)  not null default 'HEXAFLOW',
    hexaflow_run_id       varchar(100),
    updated_at            timestamptz  not null default now()
);

comment on table consultant_directory is
    'Standing cross-run consultant master kept current by the HexaFlow finance-profile '
    'pull. PII -- never log column values. Latest pull wins; used by bank_files.py '
    'for both HexaFlow-ingested and manually-uploaded cases.';
