-- Local bank-detail override layer for CSI consultants, sourced from the
-- Maybank Beneficiary Code Tracker Excel. Takes precedence over Airtable
-- (which Ikhram manages separately) in bank file generation.
-- Run against DATABASE_URL like the other db/*.sql files.

create table if not exists consultant_bank_overrides (
    employee_id                varchar(50)   primary key,  -- CSI employeeId, e.g. "HEX-0004"
    consultant_name            varchar(200)  not null,     -- as sent by HexaFlow (CSI format)
    favourite_beneficiary_code text,                       -- Maybank CMS favourite code
    bank_account_number        text,
    bank_code                  varchar(10),
    bank_name                  varchar(100),
    source                     text          not null default 'BENEFICIARY_TRACKER',
    updated_at                 timestamptz   not null default now(),
    updated_by                 text          -- who last updated this row
);

comment on table consultant_bank_overrides is
    'Local override layer for consultant bank details. Merged into the Airtable '
    'list before bank file generation — overrides win for matching employee_id. '
    'Does not modify Airtable; managed independently by the APEX team.';
