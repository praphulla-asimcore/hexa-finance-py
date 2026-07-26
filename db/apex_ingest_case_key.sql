-- Phase 1A: payload-aware APEX ingest idempotency + one case per
-- (entity, payroll period, payout cycle).
--
-- Adds two columns to payroll_cases and enforces case_key uniqueness:
--   case_key               "<ENTITY>:<YYYY-MM>:<CYCLE>" (CYCLE in 25TH/EOM/7TH),
--                          the stable identity of a payroll case. The period is
--                          the SUPPLIED payroll period; 7TH stays attached to
--                          that period even though payment lands next month.
--   ingest_payload_sha256  SHA-256 of the canonical validated ingest payload,
--                          used for payload-aware duplicate detection.
--
-- Run against the SAME database the app uses (DATABASE_URL), like the other
-- db/*.sql files. Idempotent (safe to re-run).
--
-- PRODUCTION ORDER (do not shortcut):
--   1. Apply the ADD COLUMN block below. Safe any time: both columns are
--      nullable and default NULL, so existing rows are untouched.
--   2. Run scripts/preflight_apex_case_key_readonly.py and confirm it reports
--      ZERO collisions and ZERO unclassifiable rows. This is the release gate.
--   3. Apply the unique index below. It is a PARTIAL index over non-NULL
--      case_key, so it applies cleanly even before any backfill: new ingests
--      populate case_key; legacy rows keep case_key NULL and fail closed at the
--      ingest layer (UNVERIFIED_EXISTING_RUN_REF) rather than being assumed
--      duplicates.
--
-- Do NOT backfill legacy case_key values, and do NOT merge, delete, rename, or
-- pick a winner among colliding rows here. If the preflight reports collisions
-- or unclassifiable rows, STOP and escalate: that is a separate, operator-gated
-- decision, not part of this migration.

alter table payroll_cases
    add column if not exists case_key               text,
    add column if not exists ingest_payload_sha256  varchar(64);

-- One case per canonical case_key. Partial index so legacy NULL case_key rows
-- never collide and are never forced into the constraint.
create unique index if not exists payroll_cases_case_key_unique
    on payroll_cases (case_key)
    where case_key is not null;
