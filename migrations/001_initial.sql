CREATE TABLE IF NOT EXISTS vocabulary_queue (
    id INTEGER PRIMARY KEY,
    word TEXT NOT NULL,
    normalized_word TEXT NOT NULL UNIQUE,
    context TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'imported', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error TEXT,
    anki_note_id INTEGER,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS queue_status ON vocabulary_queue(status, id);
CREATE TABLE IF NOT EXISTS queue_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO queue_state(key, value) VALUES ('sync_pending', '0');
