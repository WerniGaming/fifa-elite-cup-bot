-- Phase 26: Eigener 'nur Panel'-Kanal pro Bracket (Winner + Loser), analog zu Gruppen

ALTER TABLE tournament_bracket_meta
  ADD COLUMN IF NOT EXISTS panel_channel_id BIGINT,
  ADD COLUMN IF NOT EXISTS panel_message_id BIGINT;
