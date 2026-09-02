-- Phase 36: Spieler-Suche-Kanal (nur Vereinsmanager duerfen dort schreiben) +
-- gespeicherter Team-Registrieren-Kanal, damit die Warnung dorthin verlinken kann.

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS player_search_channel_id BIGINT,
  ADD COLUMN IF NOT EXISTS team_register_channel_id BIGINT;
