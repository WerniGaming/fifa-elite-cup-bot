-- Phase 8: Echte Spieltage mit Freigabe-Mechanik

ALTER TABLE tournament_groups
  ADD COLUMN IF NOT EXISTS released_round INTEGER NOT NULL DEFAULT 0;
