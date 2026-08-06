-- Local bank-detail override layer for CSI consultants, sourced from the
-- Maybank Beneficiary Code Tracker Excel. Takes precedence over Airtable
-- (which Ikhram manages separately) in bank file generation.
-- Run against DATABASE_URL like the other db/*.sql files.

create table if not exists consultant_bank_overrides (
    employee_id                varchar(50)   primary key,  -- CSI employeeId, e.g. "HEX-0004"
    consultant_name            varchar(200)  not null,     -- as sent by HexaFlow (CSI format) — used for identity matching, never printed if bank_account_name is set
    bank_account_name          text,                       -- name the bank account is actually registered under, when it differs (e.g. a contractor paid via their own company); printed on the bank file in place of consultant_name, but never used for identity matching
    id_number                  text,                       -- NRIC / passport / business registration number for RCGEN2 field 25/27/28
    id_type                    varchar(30),                -- 'NRIC' / 'Passport' / 'Business Registration' — authoritative for RCGEN2 field routing when set (see app/services/bank_files.py _id_fields); unset falls back to format-based guessing
    favourite_beneficiary_code text,                       -- Maybank CMS favourite code
    bank_account_number        text,
    bank_code                  varchar(10),
    bank_name                  varchar(100),
    source                     text          not null default 'BENEFICIARY_TRACKER',
    updated_at                 timestamptz   not null default now(),
    updated_by                 text          -- who last updated this row
);

-- Safe to re-run against an already-created table.
alter table consultant_bank_overrides add column if not exists bank_account_name text;
alter table consultant_bank_overrides add column if not exists id_number text;
alter table consultant_bank_overrides add column if not exists id_type varchar(30);

comment on table consultant_bank_overrides is
    'Local override layer for consultant bank details. Merged into the Airtable '
    'list before bank file generation — overrides win for matching employee_id. '
    'consultant_name is used for fraud-check identity matching (app/services/bank_files.py '
    'match_consultant); bank_account_name, when set, is what actually gets printed on the '
    'bank file instead. id_type, when set to Business Registration, forces RCGEN2 field 27 '
    'regardless of the number''s digit shape (Malaysia''s new-format 12-digit SSM company '
    'numbers are otherwise indistinguishable from a 12-digit NRIC). Does not modify Airtable; '
    'managed independently by the APEX team.';
