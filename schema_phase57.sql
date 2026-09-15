-- Erlaubt im single_bracket_mode ("nur Winner Bracket") eine feste Anzahl Teams PRO GRUPPE
-- vorzugeben (z.B. "nur 1. und 2." bei zu vielen ausgefallenen Teams/Freilosen), statt der
-- generischen Zweierpotenz-Haelfte ueber die Gesamtrangliste.
ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS single_bracket_advance_per_group integer NULL;
