-- Phase 50: Feedback-Panel-Fix (bleibt immer die letzte Nachricht im Kanal, statt
-- unter neuen Feedback-Karten zu verschwinden) + Aushilfen-System (Spieler bieten
-- sich als Ersatzspieler an, Teams koennen gezielt suchen und Kontakt aufnehmen).

ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS feedback_panel_message_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS substitute_channel_id BIGINT;
ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS substitute_panel_message_id BIGINT;

CREATE TABLE IF NOT EXISTS substitute_offers (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    discord_id BIGINT NOT NULL,
    positions TEXT[] NOT NULL,
    cup_experience TEXT NOT NULL,
    league_experience TEXT NOT NULL,
    note TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_substitute_offers_guild_active ON substitute_offers (guild_id, active);

-- Ein Spieler hat maximal EIN aktives Angebot gleichzeitig
CREATE UNIQUE INDEX IF NOT EXISTS idx_substitute_offers_one_active
    ON substitute_offers (guild_id, discord_id) WHERE active;
