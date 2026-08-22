-- Phase 4: Turniere & Anmeldung

CREATE TABLE IF NOT EXISTS tournaments (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT,
    message_id BIGINT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', -- open | closed | finished
    min_teams INTEGER NOT NULL DEFAULT 4,
    max_teams INTEGER NOT NULL DEFAULT 32,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tournament_signups (
    id SERIAL PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'registered', -- registered | waitlist | withdrawn
    signup_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tournament_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_tournament_signups_tournament ON tournament_signups(tournament_id);
CREATE INDEX IF NOT EXISTS idx_tournaments_guild ON tournaments(guild_id);
