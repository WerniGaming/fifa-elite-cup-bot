-- Phase 21: Spiel um Platz 3 jetzt auch im Loser-Bracket (nicht nur Winner-Bracket)

ALTER TABLE tournaments
  ADD COLUMN IF NOT EXISTS loser_bracket_third_id INTEGER REFERENCES teams(id);
