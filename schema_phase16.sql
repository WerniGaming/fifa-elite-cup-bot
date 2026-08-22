-- Phase 16: Kategorie-ID speichern (fuer sauberes Aufraeumen beim Turnierende)

ALTER TABLE tournaments
  ADD COLUMN IF NOT EXISTS group_category_id BIGINT;
