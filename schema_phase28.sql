-- Phase 28: Automatische Rollenvergabe fuer Vereinsmanager/Co-Manager
-- + globale Moderator-Rolle (darf Ergebnisse fuer alle Turniere verwalten,
-- ohne vollen Admin-Zugriff zu haben).

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS vm_role_id BIGINT,
  ADD COLUMN IF NOT EXISTS co_manager_role_id BIGINT,
  ADD COLUMN IF NOT EXISTS mod_role_id BIGINT;
