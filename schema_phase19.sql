-- Phase 19: Fester Speicherkanal fuer Team-Logos (dauerhafte statt ephemere CDN-Links)

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS logo_storage_channel_id BIGINT;
