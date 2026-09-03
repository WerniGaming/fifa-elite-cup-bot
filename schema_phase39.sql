-- Phase 39: Teams "aufloesen" statt hart loeschen, wenn sie schon Matches gespielt
-- haben - ein DELETE FROM teams schlaegt sonst mit einem Fremdschluessel-Fehler
-- fehl (tournament_matches.team1_id/team2_id) und wuerde bei einem Force-Delete
-- die Spielhistorie/Statistiken zerstoeren.

ALTER TABLE teams
  ADD COLUMN IF NOT EXISTS dissolved_at TIMESTAMPTZ;
