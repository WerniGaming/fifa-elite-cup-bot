-- Phase 48: Ticket-Kategorien mit unterschiedlicher Sichtbarkeit je nach Staff-Rang.
-- "Bewerbung"-Tickets nur fuer Verwaltung, "Sperren-Einspruch" ab Head Moderator,
-- alle anderen Kategorien fuer das komplette Support-Team (cup_staff_role_ids).

ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS bewerbung_role_ids BIGINT[] NOT NULL DEFAULT '{}';
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS sperre_role_ids BIGINT[] NOT NULL DEFAULT '{}';
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS verwaltung_ticket_category_id BIGINT;
