-- Phase 10: Zeitplan-Felder + Turnier-Stream-Link

ALTER TABLE tournaments
  ADD COLUMN IF NOT EXISTS minutes_per_round INTEGER NOT NULL DEFAULT 20,
  ADD COLUMN IF NOT EXISTS stream_link TEXT;
