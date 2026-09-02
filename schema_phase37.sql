-- Phase 37: Medien-Kanal (nur Bilder/Videos), Feedback-System, Freundschaftsspiel-System

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS media_only_channel_id BIGINT,
  ADD COLUMN IF NOT EXISTS feedback_channel_id BIGINT,
  ADD COLUMN IF NOT EXISTS friendly_channel_id BIGINT;

CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    author_discord_id BIGINT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    message_id BIGINT,
    channel_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feedback_votes (
    feedback_id INTEGER NOT NULL REFERENCES feedback(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    PRIMARY KEY (feedback_id, discord_id)
);

CREATE TABLE IF NOT EXISTS friendly_requests (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    requested_by_discord_id BIGINT NOT NULL,
    proposed_time TEXT,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- 'open' / 'matched' / 'withdrawn'
    matched_team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    matched_by_discord_id BIGINT,
    message_id BIGINT,
    channel_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_friendly_requests_guild_status ON friendly_requests(guild_id, status);
