-- Phase 44: Rohe Spieler-Statistiken direkt pro Match sichern (statt nur
-- ephemer bei Bracket-Abschluss aus der EA-API zu aggregieren). Grund: die
-- EA-Freundschaftsspiel-Historie ist begrenzt (rollierendes Fenster) - wenn
-- Teams zwischen Turnierstart und -ende weitere Freundschaftsspiele machen,
-- koennten aeltere Matchday-Ergebnisse aus der API-Historie fallen, bevor
-- aggregate_bracket_stats() sie am Turnierende abfragt. Deshalb wird direkt
-- nach jedem eingetragenen Ergebnis (finalize_match_result) versucht, die
-- EA-Spielerdaten fuer genau dieses Match sofort zu sichern.

CREATE TABLE IF NOT EXISTS match_player_stats (
    id SERIAL PRIMARY KEY,
    match_id INTEGER NOT NULL REFERENCES tournament_matches(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    player_name TEXT NOT NULL,
    goals INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    rating NUMERIC NOT NULL DEFAULT 0,
    mom INTEGER NOT NULL DEFAULT 0,
    saves INTEGER NOT NULL DEFAULT 0,
    position_group TEXT NOT NULL DEFAULT 'MID',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (match_id, team_id, player_name)
);

CREATE INDEX IF NOT EXISTS idx_match_player_stats_match ON match_player_stats(match_id);
