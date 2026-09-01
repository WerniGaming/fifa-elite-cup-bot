-- Phase 31: Kalender-System. Fuer alle sichtbarer, live aktualisierter Kanal
-- mit kommenden Terminen (Cups, Ligen, Sonstiges), von Admins erstellbar.
-- Gleiches Muster wie team_overview_panel (Phase 29): eigener Kanal + Liste
-- getrackter Nachrichten-IDs, komplett neu gepostet bei jeder Aenderung.

CREATE TABLE IF NOT EXISTS calendar_panel (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    message_ids BIGINT[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'sonstiges',  -- 'cup' | 'liga' | 'sonstiges'
    description TEXT,
    start_time TIMESTAMPTZ NOT NULL,
    tournament_id INTEGER REFERENCES tournaments(id) ON DELETE SET NULL,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reminder_sent BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_guild_time ON calendar_events (guild_id, start_time);
