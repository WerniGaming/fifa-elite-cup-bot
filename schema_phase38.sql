-- Phase 38: Live-Besucherzaehler fuer die Website (aktuell online + all-time).
-- Wird von der Website (nicht dem Bot) beschrieben, lebt aber in derselben
-- geteilten DB wie alles andere.

CREATE TABLE IF NOT EXISTS site_visits (
    session_id TEXT PRIMARY KEY,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_site_visits_last_seen ON site_visits(last_seen);
