-- Phase 6: Gruppenphase mit Kanälen/Rollen + Admin-Panel-Unterstützung

ALTER TABLE tournaments
  ADD COLUMN IF NOT EXISTS start_time TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS group_size INTEGER NOT NULL DEFAULT 4,
  ADD COLUMN IF NOT EXISTS advance_per_group INTEGER NOT NULL DEFAULT 2,
  ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT 'signup'; -- signup | groups | knockout | finished

CREATE TABLE IF NOT EXISTS tournament_groups (
    id SERIAL PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
    group_number INTEGER NOT NULL,
    role_id BIGINT,
    channel_id BIGINT,
    UNIQUE (tournament_id, group_number)
);

CREATE TABLE IF NOT EXISTS tournament_group_teams (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES tournament_groups(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    UNIQUE (group_id, team_id)
);

ALTER TABLE tournament_matches
  ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT 'knockout',
  ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES tournament_groups(id) ON DELETE CASCADE;

CREATE TABLE IF NOT EXISTS tournament_ko_meta (
    tournament_id INTEGER PRIMARY KEY REFERENCES tournaments(id) ON DELETE CASCADE,
    role_id BIGINT,
    channel_id BIGINT
);

CREATE INDEX IF NOT EXISTS idx_tournament_groups_tournament ON tournament_groups(tournament_id);
CREATE INDEX IF NOT EXISTS idx_tournament_group_teams_group ON tournament_group_teams(group_id);
CREATE INDEX IF NOT EXISTS idx_tournament_matches_group ON tournament_matches(group_id);
