-- Identifying a ripped film and importing its encode into Radarr (one row per rip).
CREATE TABLE film_imports (
    rip_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('identifying', 'needs_identify', 'waiting_other', 'needs_replace_ok',
                                         'importing', 'imported', 'in_jellyfin', 'failed', 'undone', 'skipped',
                                         'tv_skipped')),
    step TEXT NOT NULL DEFAULT '',
    candidates TEXT NOT NULL DEFAULT '[]',
    tmdb_id INTEGER,
    movie_id INTEGER,
    title TEXT,
    year INTEGER,
    root_folder TEXT,
    encode_id INTEGER,
    replace TEXT NOT NULL DEFAULT '{}',
    old_path TEXT,
    old_folder TEXT,
    recycled_path TEXT,
    new_path TEXT,
    extras TEXT NOT NULL DEFAULT '[]',
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    imported_at TEXT
);
CREATE INDEX film_imports_tmdb ON film_imports (tmdb_id, state);

ALTER TABLE encodes ADD COLUMN imported_path TEXT;
ALTER TABLE encodes ADD COLUMN imported_at TEXT;
