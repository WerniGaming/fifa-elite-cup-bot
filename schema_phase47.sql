-- Phase 47: Gestaffelte Bot-Berechtigungen fuer die neue 5-Rang-Staff-Struktur
-- (Trial Moderator, Moderator, Head Moderator, Administrator, Verwaltung).
-- Ersetzt/erweitert die bisherigen Einzel-Rollen-Spalten (admin_role_id, mod_role_id)
-- um Array-Varianten, damit mehrere Raenge dieselbe Berechtigungsstufe teilen koennen.

ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS admin_role_ids BIGINT[] NOT NULL DEFAULT '{}';
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS mod_role_ids BIGINT[] NOT NULL DEFAULT '{}';
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS correct_role_ids BIGINT[] NOT NULL DEFAULT '{}';
