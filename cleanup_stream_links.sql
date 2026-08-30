-- Einmaliges Aufraeumen: entfernt alle Stream-Links, die nicht dem Format
-- https://www.twitch.tv/name entsprechen.

UPDATE teams
SET stream_link = NULL
WHERE stream_link IS NOT NULL
  AND stream_link !~ '^https://www\.twitch\.tv/[A-Za-z0-9_]+/?$';

UPDATE tournaments
SET stream_link = NULL
WHERE stream_link IS NOT NULL
  AND stream_link !~ '^https://www\.twitch\.tv/[A-Za-z0-9_]+/?$';
