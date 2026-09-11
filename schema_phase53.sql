-- Erlaubt Turniere ohne Loser-Bracket (nur Winner-Bracket-KO nach der Gruppenphase).
ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS single_bracket_mode boolean NOT NULL DEFAULT false;
