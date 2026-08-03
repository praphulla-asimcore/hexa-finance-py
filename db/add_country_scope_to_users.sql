-- Scopes an arranger's exception notifications to one country (e.g. "MY", "NP").
-- NULL/empty = unscoped, receives notifications for every entity/country --
-- preserves today's behavior for any arranger not explicitly scoped.
ALTER TABLE users ADD COLUMN IF NOT EXISTS country_scope TEXT;
