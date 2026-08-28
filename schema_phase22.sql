-- Phase 22: Ticket-System

CREATE TABLE IF NOT EXISTS tickets (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    ticket_number INTEGER NOT NULL,
    channel_id BIGINT,
    opener_discord_id BIGINT NOT NULL,
    category TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open',  -- 'open' / 'closed'
    claimed_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    UNIQUE (guild_id, ticket_number)
);

CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets(guild_id);
CREATE INDEX IF NOT EXISTS idx_tickets_opener ON tickets(guild_id, opener_discord_id, status);

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS ticket_category_id BIGINT,
  ADD COLUMN IF NOT EXISTS ticket_log_channel_id BIGINT,
  ADD COLUMN IF NOT EXISTS ticket_support_role_id BIGINT,
  ADD COLUMN IF NOT EXISTS ticket_counter INTEGER NOT NULL DEFAULT 0;
