-- Phase 43: Umfrage-System (Interesse-Check, z.B. "Habt ihr Bock auf einen Cup am X?")

CREATE TABLE IF NOT EXISTS polls (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    question TEXT NOT NULL,
    description TEXT,
    created_by BIGINT NOT NULL,
    channel_id BIGINT,
    message_id BIGINT,
    closed BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    choice TEXT NOT NULL,  -- 'interested' / 'not_interested'
    voted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (poll_id, discord_id)
);
