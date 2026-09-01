-- Phase 35: Live-Ergebnis-Feed - jedes fertig gespielte Match wird sofort in
-- einen konfigurierbaren Kanal gepostet (aehnlich dem Website-Ticker).

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS results_feed_channel_id BIGINT;
