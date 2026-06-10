-- Add the `invoiced` flag to payroll_cases. Reported in the ARIA reconciliation
-- webhook payload (fire_aria_webhook) so ARIA knows whether a posted CSI run has
-- been invoiced yet. Defaults false; flipped true once invoicing is recorded.
-- Run against the SAME database the app uses (DATABASE_URL), like the other db/*.sql.

alter table payroll_cases
    add column if not exists invoiced boolean not null default false;
