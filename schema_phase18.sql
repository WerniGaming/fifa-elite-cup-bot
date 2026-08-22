-- Phase 18: Qualifikationsrunde statt Freilose in der KO-Phase

ALTER TABLE tournament_bracket_meta
  ADD COLUMN IF NOT EXISTS direct_entrants INTEGER[];

ALTER TABLE tournament_matches
  ADD COLUMN IF NOT EXISTS is_third_place_match BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE tournaments
  ADD COLUMN IF NOT EXISTS winner_bracket_third_id INTEGER REFERENCES teams(id);
