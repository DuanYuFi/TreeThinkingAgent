PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL DEFAULT 'default',
    is_root INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 50),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'ready', 'active', 'blocked', 'done')),
    notes TEXT,
    importance TEXT NOT NULL DEFAULT 'medium' CHECK (importance IN ('low', 'medium', 'high')),
    confidence REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
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
    min_importance TEXT NOT NULL DEFAULT 'medium',
    min_confidence REAL NOT NULL,
    max_tasks INTEGER NOT NULL,
    inserted_count INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);
CREATE INDEX IF NOT EXISTS idx_task_edges_source ON task_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_task_edges_target ON task_edges(target_id);
