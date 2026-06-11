-- Per-client required-document profile. Defines which supporting documents each
-- HSSB customer must provide for a CSI run, so the document-gate checks can become
-- client-aware (a "payroll report" client must not be flagged MISSING_TIMESHEET; a
-- timesheet client must have a client-signed timesheet; etc.). Sourced from the
-- "HSSB_Customer List and Supporting Docs" workbook.
--
-- One ACTIVE profile per (client_name_csi, invoicing_currency): Floward and Global
-- Convergence Inc bill in both USD and MYR, so currency is part of the key. History
-- is kept by setting effective_to on a superseded row (the unique index only covers
-- the current/active row, where effective_to is null).
-- Run against the SAME database the app uses (DATABASE_URL), like the other db/*.sql.

create table if not exists client_document_profiles (
    id                          uuid primary key default gen_random_uuid(),
    client_name_csi             varchar(200) not null,
    client_name_zoho            varchar(200),
    entity                      varchar(20) not null default 'HSSB',
    invoicing_from              varchar(200),
    invoicing_currency          varchar(10) default 'MYR',
    work_order_required         boolean not null default true,
    timesheet_required          boolean not null default false,
    payroll_report_required     boolean not null default false,
    po_required                 boolean not null default false,
    hiring_note_required        boolean not null default false,
    letter_to_hire_required     boolean not null default false,
    wcn_required                boolean not null default false,
    approved_costing_required   boolean not null default false,
    payment_blocked_without_docs boolean not null default true,
    notes                       text,
    effective_from              date not null default current_date,
    effective_to                date,
    created_at                  timestamptz default now()
);

create unique index if not exists client_doc_profile_unique
    on client_document_profiles (client_name_csi, invoicing_currency)
    where effective_to is null;

-- All rows: work_order_required stays true (column default). payment_blocked_without_docs
-- stays true (default). Only the document booleans that apply per client are set true.
-- Column order below:
--   client_name_csi, client_name_zoho, entity, invoicing_from, invoicing_currency,
--   timesheet_required, payroll_report_required, po_required, hiring_note_required,
--   letter_to_hire_required, wcn_required, approved_costing_required
insert into client_document_profiles
    (client_name_csi, client_name_zoho, entity, invoicing_from, invoicing_currency,
     timesheet_required, payroll_report_required, po_required, hiring_note_required,
     letter_to_hire_required, wcn_required, approved_costing_required)
values
    ('Acclime Management Services', 'Acclime Management Services Sdn Bhd', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('Agensi Kounseling Dan Pengurusan Kredit', 'Agensi Kaunseling dan Pengurusan Kredit', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, false, true,  false, false, false),
    ('Aligned Automation', 'Aligned Automation', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'USD', false, true,  false, false, false, false, false),
    ('Bank Negara Malaysia', 'Bank Negara Malaysia', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, false, false, true,  false, false),
    ('CIMB Bank Berhad', 'CIMB Bank Berhad', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, true,  false, false, false, false),
    ('Circadia Corporate Services', 'Circadia Corporate Services Sdn.Bhd.', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('Digital Nasional Berhad', 'Digital Nasional Berhad', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, true,  false, false, false, false),
    ('Edgepoint Infrastructure', 'Edgepoint Infrastructure Sdn Bhd', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, false, false, false, false, false),
    ('Edgepoint Towers', 'EdgePoint Towers Sdn. Bhd.', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, true,  false, false, false, true),
    ('Floward Technology Malaysia', 'Floward International General Trading Co. W.L.L', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'USD', false, true,  false, false, false, false, false),
    ('Floward Technology Malaysia', 'Floward International General Trading Co. W.L.L', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('Global Convergence Inc', 'Global Convergence Inc.', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'USD', true,  false, true,  false, false, false, false),
    ('Global Convergence Inc', 'Global Convergence Inc.', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, true,  false, false, false, false),
    ('Global Convergence Ireland Operations Limited', 'Global Convergence Ireland Operations Limited', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, true,  false, false, false, false),
    ('GTP Network Sdn Bhd', 'GTP Network Sdn Bhd', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, false, false, false, false, false),
    ('Horizons', 'Horizon Global Technology Pte. Ltd.', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'USD', false, true,  false, false, false, false, false),
    ('Multiplier Technologies', 'Multiplier Technologies Pte. Ltd.', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'USD', false, true,  false, false, false, false, false),
    ('Nanotek Solutions', 'Nanotek Solutions Sdn Bhd', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('Native Teams Limited', 'Native Teams Limited', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('Ninjacart Global Services', 'NINJACART GLOBAL SERVICES SDN. BHD.', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('Nokia Services & Networks', 'Nokia Services and Networks Malaysia Sdn Bhd', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, true,  false, false, true,  false),
    ('Quality Kiosk Technologies (Malaysia)', 'QK TECHNOLOGIES MALAYSIA SDN. BHD', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('Reans Consultancy', 'Reans Consulting Sdn. Bhd.', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('Sandvik', 'Sandvik Equipment Sdn. Bhd.', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, false, true,  false, false, false),
    ('Skuad', 'Skuad Pte Limited', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('The Walnut.ai', 'The Walnut.ai Pte Ltd', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, false, true,  false, false, false),
    ('Wuhan Fiberhome International (M)', 'Wuhan Fiberhome International (Malaysia) Sdn Bhd', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, true,  false, false, false, false),
    ('Marco Global Payroll', 'Marco Global Payroll Pte. Ltd', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('Sparsa AI', 'Sparsa AI Pte Ltd', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('PT. Inovasi Digital Untuk Transformasi', 'PT Inovasi Digital Untuk Transformasi', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('Dezan Shira & Associates', 'Dezan Shira & Associates Malaysia Sdn Bhd', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', false, true,  false, false, false, false, false),
    ('Perteuman Sdn Bhd', 'Pertemuan Global Sdn. Bhd. (GCI)', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'MYR', true,  false, true,  false, false, false, false),
    ('WHA Nominees', 'WHA Nominees Limited', 'HSSB', 'Hexamatics Servcomm Sdn Bhd', 'USD', false, true,  false, false, false, false, false),
    ('Elpress BV', 'Elpress BV', 'HCI', 'Hexamatics Consulting Inc.', 'USD', false, true,  false, false, false, false, false),
    ('Subex (Asia Pacific)', 'Subex (Asia Pacific) Pte Limited', 'HSPL', 'Hexamatics Singapore Pte Ltd', 'MYR', false, true,  false, false, false, false, false),
    ('Haleon', 'Haleon Malaysia Sdn Bhd', 'KISB', 'Karya Indah Sdn Bhd', 'MYR', true,  false, true,  false, false, false, false),
    ('SACOFA', 'Sacofa Sdn Bhd', 'KISB', 'Karya Indah Sdn Bhd', 'MYR', true,  false, false, false, false, false, false)
on conflict (client_name_csi, invoicing_currency) where effective_to is null do nothing;
