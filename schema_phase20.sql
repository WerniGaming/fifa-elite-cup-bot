-- Phase 20: Konfigurierbare Admin-Rolle (zusaetzlich zu echten Server-Administratoren)

ALTER TABLE guild_settings
  ADD COLUMN IF NOT EXISTS admin_role_id BIGINT;
