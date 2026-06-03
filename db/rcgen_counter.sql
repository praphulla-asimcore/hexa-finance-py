-- Persistent atomic counters (e.g. the RCGEN bank-file running number).
-- Run this once in the Supabase SQL editor before enabling direct .txt generation.
-- The RCGEN running number is the DB equivalent of the macro's
-- 'runningnumber' sheet (cell A2); Maybank CMS uses it for anti-replay, so it
-- must never go backwards or repeat a value the bank has already accepted.

create table if not exists app_counters (
    key   text primary key,
    value bigint not null default 0
);

-- Seed the Domestic Payments running number.
-- IMPORTANT: set the value to AT LEAST the maker's current macro running number
-- (open the RCGEN2 workbook → 'runningnumber' sheet → cell A2) so we never reuse
-- a number the bank has already seen. 1000 is a safe default if the macro count
-- is below it; raise it if not.
insert into app_counters (key, value)
values ('rcgen_dp', 1000)
on conflict (key) do nothing;

-- Atomic increment-and-return. The app calls this once per generated .txt.
create or replace function next_counter(p_key text)
returns bigint
language plpgsql
as $$
declare
    v bigint;
begin
    update app_counters set value = value + 1 where key = p_key returning value into v;
    if v is null then
        insert into app_counters (key, value) values (p_key, 1) returning value into v;
    end if;
    return v;
end;
$$;
