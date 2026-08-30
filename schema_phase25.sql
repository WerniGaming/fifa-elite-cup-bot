-- Phase 25: Eigener 'nur Panel'-Kanal pro Gruppe (immer sauber, kein Chat-Verlauf)

ALTER TABLE tournament_groups
  ADD COLUMN IF NOT EXISTS panel_channel_id BIGINT;
