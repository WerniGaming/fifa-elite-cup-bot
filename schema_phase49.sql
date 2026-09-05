-- Phase 49: Staff-Uebersicht - ein persistentes Panel, das automatisch alle Mitglieder
-- der 5 Staff-Raenge (Trial Moderator bis Verwaltung) auflistet und sich selbst
-- aktualisiert, sobald jemand eine dieser Rollen bekommt oder verliert.

CREATE TABLE IF NOT EXISTS staff_overview_panel (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL
);
