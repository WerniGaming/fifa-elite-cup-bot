-- Phase 30: Logo-URL-Ablauf beheben. Discord-CDN-Attachment-URLs enthalten eine
-- signierte Gueltigkeit (ex=/is=/hm=-Parameter), die nach ca. 24h ablaeuft - auch
-- wenn die Nachricht selbst dauerhaft in einem "Speicher-Kanal" liegt. Nur ein
-- erneutes Abrufen der Nachricht liefert eine frische, gueltige URL. Deshalb wird
-- zusaetzlich zur URL (als Cache) die Kanal-/Nachrichten-Referenz gespeichert,
-- damit ein periodischer Hintergrund-Task die URL regelmaessig auffrischen kann.

ALTER TABLE teams
  ADD COLUMN IF NOT EXISTS logo_channel_id BIGINT,
  ADD COLUMN IF NOT EXISTS logo_message_id BIGINT;
