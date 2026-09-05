-- Phase 52: KO-Bracket-Kanaele (winner-bracket, looser-bracket + deren Panel-Kanaele)
-- bekommen eine eigene Kategorie statt lose auf Server-Root zu liegen.

ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS bracket_category_id BIGINT;
