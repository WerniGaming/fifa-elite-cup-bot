-- Phase 23: Spendenturnier - automatischer Zahlungs-Kanal bei Anmeldung

ALTER TABLE tournaments
  ADD COLUMN IF NOT EXISTS is_donation_tournament BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS donation_info TEXT;

ALTER TABLE tickets
  ADD COLUMN IF NOT EXISTS tournament_id INTEGER REFERENCES tournaments(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS payment_status TEXT;  -- NULL fuer normale Tickets, 'pending'/'claimed'/'confirmed' fuer Zahlungs-Tickets
