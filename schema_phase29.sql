-- Phase 29: Live-Vereins-Uebersicht (ein Kanal, mehrere automatisch verwaltete
-- Nachrichten - eine reicht bei vielen Teams nicht wegen Components-V2-Zeichenlimit)

CREATE TABLE IF NOT EXISTS team_overview_panel (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    message_ids BIGINT[] NOT NULL DEFAULT '{}'
);
