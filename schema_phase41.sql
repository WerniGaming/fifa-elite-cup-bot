-- Phase 41: Freundschaftsspiel-Panel wird zu einem einzigen, dauerhaft aktuellen
-- Uebersichts-Panel (wie der Kalender) statt vieler einzelner Anfrage-Nachrichten,
-- die das eigentliche Panel im Kanal nach oben verdraengen. Braucht Message-Tracking
-- wie calendar_panel/stream_list.

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS friendly_panel_message_id BIGINT;
