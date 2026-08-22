-- Phase 14: Oeffentlicher Sperren-Log-Kanal

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS bans_log_channel_id BIGINT;
