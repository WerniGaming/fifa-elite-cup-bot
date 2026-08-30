-- Phase 27: 'Team ist da' zieht von der Signup-Phase (vor Auslosung) in die
-- einzelnen Gruppen um (nach Auslosung) - blockiert jetzt "Spieltag 1 freigeben"
-- statt "Gruppenphase starten".

ALTER TABLE tournament_group_teams
  ADD COLUMN IF NOT EXISTS confirmed_ready BOOLEAN NOT NULL DEFAULT false;
