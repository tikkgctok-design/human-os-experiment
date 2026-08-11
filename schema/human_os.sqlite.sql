PRAGMA foreign_keys = ON;

CREATE TABLE objects (
    object_id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    source TEXT,
    source_id TEXT,
    occurred_at DATETIME,
    captured_at DATETIME,
    parent_id TEXT,
    raw_uri TEXT NOT NULL,
    content_hash TEXT,
    mime_type TEXT,
    topic TEXT,
    event_id TEXT,
    confidence REAL,
    created_at DATETIME NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES objects(object_id)
);

CREATE UNIQUE INDEX idx_objects_source_source_id
    ON objects(source, source_id)
    WHERE source IS NOT NULL AND source_id IS NOT NULL;

CREATE INDEX idx_objects_occurred_at ON objects(occurred_at);
CREATE INDEX idx_objects_captured_at ON objects(captured_at);
CREATE INDEX idx_objects_event_id ON objects(event_id);
CREATE INDEX idx_objects_hash ON objects(content_hash);

CREATE TABLE relations (
    relation_id TEXT PRIMARY KEY,
    from_object_id TEXT NOT NULL,
    to_object_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    confidence REAL,
    model_id TEXT,
    created_at DATETIME NOT NULL,
    FOREIGN KEY (from_object_id) REFERENCES objects(object_id),
    FOREIGN KEY (to_object_id) REFERENCES objects(object_id)
);

CREATE INDEX idx_relations_from ON relations(from_object_id);
CREATE INDEX idx_relations_to ON relations(to_object_id);
CREATE INDEX idx_relations_type ON relations(relation_type);

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at DATETIME NOT NULL
);

CREATE TABLE import_runs (
    import_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    raw_uri TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    started_at DATETIME NOT NULL,
    completed_at DATETIME,
    status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
    stats_json TEXT,
    error TEXT
);

CREATE INDEX idx_import_runs_source_hash ON import_runs(source, source_hash);

CREATE TABLE object_versions (
    object_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    import_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    occurred_at DATETIME,
    captured_at DATETIME NOT NULL,
    parent_id TEXT,
    raw_uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mime_type TEXT,
    topic TEXT,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (object_id, version),
    UNIQUE (object_id, content_hash),
    FOREIGN KEY (object_id) REFERENCES objects(object_id),
    FOREIGN KEY (import_id) REFERENCES import_runs(import_id),
    FOREIGN KEY (parent_id) REFERENCES objects(object_id)
);

CREATE INDEX idx_object_versions_import ON object_versions(import_id);
CREATE INDEX idx_object_versions_hash ON object_versions(content_hash);

CREATE TABLE import_diagnostics (
    diagnostic_id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('warning', 'error')),
    code TEXT NOT NULL,
    source_id TEXT,
    detail TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    FOREIGN KEY (import_id) REFERENCES import_runs(import_id)
);

CREATE INDEX idx_import_diagnostics_import ON import_diagnostics(import_id);

INSERT INTO schema_migrations(version, applied_at)
VALUES (2, CURRENT_TIMESTAMP);

PRAGMA user_version = 2;
