-- Erlaubt einen manuellen Override der Bracket-Groesse (umgeht die feste Stufenliste),
-- damit ein Admin bei "1 Team fehlt noch" sofort mit Freilos auffuellen und starten kann,
-- statt auf eine weitere echte Anmeldung warten zu muessen.
ALTER TABLE tournaments ADD COLUMN IF NOT EXISTS custom_bracket_size integer NULL;
