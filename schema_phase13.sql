-- Phase 13: Team-Sperren zusaetzlich zu Spieler-Sperren

CREATE TABLE IF NOT EXISTS banned_teams (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    reason TEXT,
    banned_by BIGINT NOT NULL,
    banned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ, -- NULL = permanent
    UNIQUE (guild_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_banned_teams_guild ON banned_teams(guild_id);
