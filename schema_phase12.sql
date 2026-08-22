-- Phase 12: Sperren-System (Spieler-Sperren pro Server)

CREATE TABLE IF NOT EXISTS banned_users (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    discord_id BIGINT NOT NULL,
    reason TEXT,
    banned_by BIGINT NOT NULL,
    banned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ, -- NULL = permanent
    UNIQUE (guild_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_banned_users_guild ON banned_users(guild_id);
