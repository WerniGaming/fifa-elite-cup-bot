-- Phase 33: Audit-Log. Zeichnet zentral auf, wer wann was gemacht hat
-- (Team-/Turnier-Verwaltung, Bans, Kalender, Anmeldungen) - fuer Admins
-- als Nachvollziehbarkeit, sowohl im Discord-Panel als auch auf der Website.

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    actor_discord_id BIGINT,
    actor_name TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    details TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_guild_time ON audit_log (guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log (action);
