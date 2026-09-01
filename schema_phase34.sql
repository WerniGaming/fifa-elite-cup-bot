-- Phase 34: Live-Log-Kanal fuer das Audit-Log - jede Aktion wird zusaetzlich
-- sofort in diesen Kanal gepostet (statt nur auf Abruf per /audit_log).

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS audit_log_channel_id BIGINT;
