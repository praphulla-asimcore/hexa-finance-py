-- Standing Nepal (HNPL) employee master -- kept separate from
-- consultant_master by design (see 2026-07-31 conversation): Malaysia's
-- table carries Talenox-specific assumptions (HEX-#### apex-id regex,
-- NRIC-vs-Passport inference) that don't apply to RigoHR's data shape.
--
-- Surrogate uuid primary key, NOT employee_ledger_code -- the initial 2026-07
-- manual load has employee_ledger_code blank for every row (RigoHR's own
-- employee key, returned as EmployeeLedgerCode on GroupedBy=Employee rows
-- from GET /salary-vouchers, e.g. "123-109"; to be filled in later directly
-- in APEX). A partial unique index enforces no duplicates once it IS filled,
-- without blocking many-NULL rows today.
--
-- apex_employee_id is the resolved HEX-xxxx id used to match this employee
-- against an ingested RigoHR payroll run, mirroring
-- consultant_master.apex_employee_id / app/services/bank_files.py's
-- match_consultant. Null until resolved.
--
-- ssf_number/cit_number are the employee's own SSF/CIT REGISTRATION
-- numbers (for statutory filing reference), not the contribution AMOUNT --
-- amounts come from RigoHR per pay period. Tentative pending the full
-- journal-entry account-name sample; may need revision once that lands.
--
-- Run against the SAME database the app uses (DATABASE_URL), like the
-- other db/*.sql files.

create table if not exists nepal_employee_master (
    id                      uuid          primary key default gen_random_uuid(),
    entity                  varchar(20)   not null default 'HNPL',
    employee_ledger_code    varchar(50),                  -- RigoHR EmployeeLedgerCode, e.g. "123-109"; blank until backfilled
    apex_employee_id        varchar(50),                  -- resolved HEX-xxxx APEX id, null until known
    employee_name           varchar(200),
    citizenship_number      text,                         -- Nepali national ID (Nagarikta); blank for some interns pending issuance
    pan_number               text,                         -- tax Permanent Account Number
    nationality              varchar(100)  default 'Nepali',
    bank_name                varchar(100),
    bank_code                varchar(10),                  -- blank until a Nepal bank-code list is confirmed
    bank_account_name        text,
    bank_account_number      text,
    ssf_number                text,                         -- Social Security Fund registration number; blank when not required (retirees/interns)
    cit_number                text,                         -- Citizen Investment Trust account number
    resign_date               date,
    source                    varchar(30)   not null default 'MANUAL_ENTRY',
    imported_at               timestamptz   not null default now()
);

create index if not exists nepal_employee_master_apex_id_idx on nepal_employee_master (apex_employee_id);
create unique index if not exists nepal_employee_master_ledger_code_uq
    on nepal_employee_master (employee_ledger_code) where employee_ledger_code is not null;
create unique index if not exists nepal_employee_master_pan_uq
    on nepal_employee_master (pan_number) where pan_number is not null;

comment on table nepal_employee_master is
    'Standing Nepal (HNPL) employee master, manually maintained. Separate from '
    'consultant_master (Malaysia-specific assumptions don''t apply). Bank-detail '
    'and identity source for HNPL once RigoHR ingestion is built.';
