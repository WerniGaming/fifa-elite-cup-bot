-- Phase 42: Aufgeloeste Teams (dissolved_at gesetzt) sollen ihren Namen nicht mehr fuer
-- immer blockieren - seit Phase 39 (Team aufloesen statt hart loeschen) verhinderte die
-- alte UNIQUE(guild_id, name)-Constraint, dass jemand ein neues Team mit demselben Namen
-- registriert, obwohl das alte Team laengst nicht mehr aktiv ist (live bei "CreativeMinds"
-- gemeldet). Ersetzt durch einen partiellen Unique-Index, der nur AKTIVE Teams eindeutig haelt.

ALTER TABLE teams DROP CONSTRAINT IF EXISTS teams_guild_id_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS teams_guild_id_name_active_key ON teams (guild_id, name) WHERE dissolved_at IS NULL;
