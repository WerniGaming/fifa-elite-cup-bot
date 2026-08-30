-- Phase 24: Stream-Link-Uebersicht (live aktualisierte Liste aller Team-Streams)

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS stream_list_channel_id BIGINT,
  ADD COLUMN IF NOT EXISTS stream_list_message_id BIGINT;
