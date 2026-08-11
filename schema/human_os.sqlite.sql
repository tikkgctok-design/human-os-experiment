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
