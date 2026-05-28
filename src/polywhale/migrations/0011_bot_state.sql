-- Persistent state for the telegram command bot (last_update_id, etc).
-- Used by telegram_commander.py to avoid reprocessing the same /command.

CREATE TABLE bot_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);
