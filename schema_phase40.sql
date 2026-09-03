-- Phase 40: Freundschaftsspiel-System auf mehrere Zeitslots pro Anfrage +
-- mehrere Bewerber-Teams pro Slot umgebaut (vorher: 1 Anfrage = 1 Zeitpunkt =
-- 1:1-Zusage). Der Ersteller kann jetzt mehrere Zeiten gleichzeitig ausschreiben
-- und pro Zeit aus mehreren interessierten Teams eines auswaehlen.

ALTER TABLE friendly_requests
  DROP COLUMN IF EXISTS proposed_time,
  DROP COLUMN IF EXISTS matched_team_id,
  DROP COLUMN IF EXISTS matched_by_discord_id;

CREATE TABLE IF NOT EXISTS friendly_slots (
    id SERIAL PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES friendly_requests(id) ON DELETE CASCADE,
    proposed_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',  -- 'open' / 'matched'
    matched_team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    matched_by_discord_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS friendly_candidates (
    id SERIAL PRIMARY KEY,
    slot_id INTEGER NOT NULL REFERENCES friendly_slots(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (slot_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_friendly_slots_request ON friendly_slots(request_id);
CREATE INDEX IF NOT EXISTS idx_friendly_candidates_slot ON friendly_candidates(slot_id);
