-- Phase 17: KRITISCHER FIX - die urspruengliche UNIQUE-Regel auf tournament_matches
-- kannte nur (tournament_id, round, match_number). Das kollidierte zwangslaeufig,
-- sobald Gruppenphase UND KO-Phase (Winner+Loser) jeweils bei Runde 1 / Match 1
-- anfingen zu zaehlen - die Datenbank hat das zweite Insert blockiert.

ALTER TABLE tournament_matches
  DROP CONSTRAINT IF EXISTS tournament_matches_tournament_id_round_match_number_key;

ALTER TABLE tournament_matches
  ADD CONSTRAINT tournament_matches_unique_slot UNIQUE (tournament_id, phase, bracket, round, match_number);
