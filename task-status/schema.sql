PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS inquiries (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate', 'ready', 'active', 'blocked', 'done')),
    answer TEXT,
    importance REAL NOT NULL DEFAULT 0.0 CHECK (importance >= 0.0 AND importance <= 1.0),
    confidence REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inquiry_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES inquiries(id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK (relation IN ('decomposes_to', 'depends_on', 'relates_to')),
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_id, target_id, relation),
    CHECK (source_id != target_id)
);

CREATE TABLE IF NOT EXISTS conversation_ingests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_hash TEXT NOT NULL UNIQUE,
    source_label TEXT,
    provider TEXT,
    model_name TEXT,
    min_importance REAL NOT NULL,
    min_confidence REAL NOT NULL,
    max_inquiries INTEGER NOT NULL,
    inserted_count INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inquiries_status ON inquiries(status);
CREATE INDEX IF NOT EXISTS idx_inquiries_updated_at ON inquiries(updated_at);
CREATE INDEX IF NOT EXISTS idx_edges_source ON inquiry_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON inquiry_edges(target_id);
