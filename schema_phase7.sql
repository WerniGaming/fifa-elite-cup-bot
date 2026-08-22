-- Phase 7: Ergebnis-Eintragung mit Bestaetigung + Toren

ALTER TABLE tournament_matches
  ADD COLUMN IF NOT EXISTS team1_score INTEGER,
  ADD COLUMN IF NOT EXISTS team2_score INTEGER,
  ADD COLUMN IF NOT EXISTS reported_by_team_id INTEGER REFERENCES teams(id),
  ADD COLUMN IF NOT EXISTS pending_confirmation BOOLEAN NOT NULL DEFAULT false;
