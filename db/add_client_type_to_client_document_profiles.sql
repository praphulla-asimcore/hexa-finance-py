-- APC (advance paying) / CC (credit) client type per client profile. Drives the
-- CSI accrual's APC-/CC- expense accounts and whether revenue is accrued (APC
-- clients are billed in advance, never accrued). NULL = not set → the app falls
-- back to HexaFlow's per-consultant client_type, then CC.
-- The app also runs this idempotently on first use (admin._ensure_client_type_column).
alter table client_document_profiles
    add column if not exists client_type varchar(3)
    check (client_type is null or client_type in ('APC', 'CC'));
