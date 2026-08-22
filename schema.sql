-- Phase 1: Teams & Team Manager
-- Weitere Tabellen (tournaments, matches, ...) kommen in späteren Phasen dazu.

CREATE TABLE IF NOT EXISTS teams (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    ea_club_id TEXT,
    ea_club_name TEXT,
    ea_platform TEXT NOT NULL DEFAULT 'common-gen5',
    stream_link TEXT,
    logo_url TEXT,
    owner_discord_id BIGINT NOT NULL,
    notifications_enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guild_id, name)
);

CREATE TABLE IF NOT EXISTS team_managers (
    id SERIAL PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    role TEXT NOT NULL DEFAULT 'co_manager', -- 'owner' | 'co_manager'
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (team_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_team_managers_discord_id ON team_managers(discord_id);
CREATE INDEX IF NOT EXISTS idx_teams_guild_id ON teams(guild_id);
