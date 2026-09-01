-- Phase 32: dauerhafte Spieler-Statistiken (fuer die neue Website-Statistikseite).
-- Ein Snapshot pro Team+Bracket+Spieler je Turnier - wird bei jedem Awards/Top11-Lauf
-- per UPSERT ueberschrieben (kein Mehraufwand an EA-API-Calls, nutzt die eh schon
-- abgerufenen Daten aus aggregate_bracket_stats).

CREATE TABLE IF NOT EXISTS tournament_player_stats (
    id SERIAL PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    bracket TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    player_name TEXT NOT NULL,
    matches INTEGER NOT NULL DEFAULT 0,
    goals INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    mom INTEGER NOT NULL DEFAULT 0,
    saves INTEGER NOT NULL DEFAULT 0,
    avg_rating NUMERIC(4,2) NOT NULL DEFAULT 0,
    position_group TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tournament_id, bracket, team_id, player_name)
);

CREATE INDEX IF NOT EXISTS idx_tps_player ON tournament_player_stats (player_name);
CREATE INDEX IF NOT EXISTS idx_tps_tournament ON tournament_player_stats (tournament_id);
