-- 0002_signal_failure_reason.sql — record broker-side order failures.
--
-- Apply with: wrangler d1 execute forex-signals-db --file=./migrations/0002_signal_failure_reason.sql
-- Additive only: a single nullable column. Existing data untouched.
--
-- NOTE: D1/SQLite has no "ADD COLUMN IF NOT EXISTS". If this migration
-- was already applied once, applying it again errors harmlessly on the
-- duplicate column — safe to retry, just confirm the column exists.

ALTER TABLE signals ADD COLUMN failure_reason TEXT;  -- detail from POST /signals/:id/failed
