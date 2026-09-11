-- Erlaubt pro Turnier eine abweichende feste Gruppengroesse (z.B. 6er statt 4er-Gruppen).
ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS group_size_override integer NULL;
