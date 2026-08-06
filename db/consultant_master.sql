-- Standing consultant master sourced from the Talenox HR export
-- ("Profiles-employees-export-Hexamatics .xlsx"), replacing Airtable as a
-- bank-detail data source. See app/services/consultant_master_import.py for
-- the import pipeline and scripts/import_consultant_master.py for the CLI.
--
-- Keyed by (entity, employee_id) rather than employee_id alone: the Talenox
-- export's employee_id is a mix of already-assigned APEX ids (HEX-0030) and
-- entity-native ids (HC-24, 358) for people not yet onboarded into APEX, and
-- is NOT guaranteed unique even within one sheet (e.g. HC-01 appears twice in
-- HEDU for two different people) -- true in-sheet duplicates are excluded
-- from import and reported, never silently merged under a shared key.
--
-- apex_employee_id is the resolved HEX-xxxx id used to match this consultant
-- against CSI/payroll rows (see app/services/bank_files.py match_consultant).
-- It is null until resolved -- a consultant genuinely new to APEX simply
-- won't be matched by any case yet, same as today's Airtable/HexaFlow gap.
--
-- Never carries a Favourite Beneficiary Code -- that field only ever lives in
-- consultant_bank_overrides (a Maybank CMS artifact assigned after manual
-- registration), keyed by apex_employee_id.
-- Run against the SAME database the app uses (DATABASE_URL), like the other db/*.sql.

create table if not exists consultant_master (
    entity                 varchar(20)   not null,       -- HSSB / HCSSB / HEDU / DATACRATS
    employee_id            varchar(50)   not null,       -- raw Talenox id (HEX-xxxx, HC-xx, or bare number)
    apex_employee_id        varchar(50),                  -- resolved HEX-xxxx APEX id, null until known
    consultant_name         varchar(200),
    ic_number               text,
    ic_type                 varchar(30),                  -- NRIC / Passport / Business Registration (inferred if unset)
    nationality             varchar(100),
    bank_name               varchar(100),
    bank_code               varchar(10),
    bank_account_name       text,
    bank_account_number     text,
    epf_number              text,
    tin_number              text,                         -- pcb_number
    resign_date             date,
    source                  varchar(30)   not null default 'TALENOX_EXPORT',
    imported_at             timestamptz   not null default now(),
    primary key (entity, employee_id)
);

create index if not exists consultant_master_apex_id_idx on consultant_master (apex_employee_id);

-- Safe to re-run: widens ic_type from its original varchar(20), which was too
-- narrow for the "Business Registration" ID type value (21 chars) and caused
-- StringDataRightTruncation on save once that option was added to the admin UI.
alter table consultant_master alter column ic_type type varchar(30);

comment on table consultant_master is
    'Standing consultant master imported from the Talenox HR export. Bank-detail '
    'precedence in bank_files.py: consultant_bank_overrides > consultant_directory '
    '(HexaFlow) > consultant_master (this table) > Airtable.';
