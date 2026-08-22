-- Phase 11: Server-weite Stats-Kanäle (Winner-Top3, Loser-Top3, Awards, Top-11)

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    winner_top3_channel_id BIGINT,
    loser_top3_channel_id BIGINT,
    awards_channel_id BIGINT,
    top11_channel_id BIGINT
);
