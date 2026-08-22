-- Phase 9: Winner-Bracket + Loser-Bracket statt einer einzelnen KO-Phase

ALTER TABLE tournaments
  ADD COLUMN IF NOT EXISTS loser_advance_per_group INTEGER NOT NULL DEFAULT 3,
  ADD COLUMN IF NOT EXISTS winner_champion_id INTEGER REFERENCES teams(id),
  ADD COLUMN IF NOT EXISTS loser_champion_id INTEGER REFERENCES teams(id);

ALTER TABLE tournaments ALTER COLUMN advance_per_group SET DEFAULT 3;

ALTER TABLE tournament_matches
  ADD COLUMN IF NOT EXISTS bracket TEXT NOT NULL DEFAULT 'winner'; -- winner | loser

CREATE TABLE IF NOT EXISTS tournament_bracket_meta (
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    bracket TEXT NOT NULL, -- winner | loser
    role_id BIGINT,
    channel_id BIGINT,
    PRIMARY KEY (tournament_id, bracket)
);
