-- Phase 51: Aushilfen-System komplett neu, nach dem Vorbild des Freundschaftsspiel-Panels -
-- EIN Panel mit zwei Kategorien (Aushilfen bieten sich an / Teams suchen eine Aushilfe),
-- Bewerben per Dropdown statt eigener Nachrichten pro Anfrage (die verschwinden sonst
-- nach oben, sobald mehrere Anfragen/Angebote gepostet werden).

DROP TABLE IF EXISTS substitute_offers CASCADE;

CREATE TABLE substitute_offers (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    discord_id BIGINT NOT NULL,
    positions TEXT[] NOT NULL,
    cup_experience TEXT NOT NULL,
    league_experience TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'open', -- open | matched | withdrawn
    matched_team_id INTEGER REFERENCES teams(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_substitute_offers_guild_status ON substitute_offers (guild_id, status);

CREATE TABLE substitute_offer_candidates (
    id SERIAL PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES substitute_offers(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    discord_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (offer_id, team_id)
);

CREATE TABLE substitute_requests (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    requested_by_discord_id BIGINT NOT NULL,
    positions TEXT[] NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open', -- open | matched | withdrawn
    matched_discord_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_substitute_requests_guild_status ON substitute_requests (guild_id, status);

CREATE TABLE substitute_request_candidates (
    id SERIAL PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES substitute_requests(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (request_id, discord_id)
);
