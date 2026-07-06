-- Apex -> HexaFlow Consultant Finance Profile pull (Pack 5) status marker on
-- payroll_cases. Set to 'pending' when the pull starts, then flipped to
-- 'succeeded'/'failed' once the GET .../consultant-finance-profiles call to
-- HexaFlow resolves. NULL means "not applicable" -- either a CSI-Generator-only
-- payload with no hexaflow_csi_run_id, or the feature is unconfigured.
-- Run against the SAME database the app uses (DATABASE_URL), like the other db/*.sql.

alter table payroll_cases
    add column if not exists finance_profile_pull_status    varchar(20),
    add column if not exists finance_profile_count           integer,
    add column if not exists finance_profile_pulled_at       timestamptz,
    add column if not exists finance_profile_missing_counts  jsonb;
