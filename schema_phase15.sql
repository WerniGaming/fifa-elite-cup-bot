-- Phase 15: Aktivitaetscheck, live aktualisierte Gruppen-Panels, Live-Spielplan-Kanal

ALTER TABLE tournament_signups
  ADD COLUMN IF NOT EXISTS confirmed_active BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE tournament_groups
  ADD COLUMN IF NOT EXISTS panel_message_id BIGINT;

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS live_schedule_channel_id BIGINT;

ALTER TABLE tournaments
  ADD COLUMN IF NOT EXISTS live_schedule_message_id BIGINT;
