ALTER TABLE drives ADD COLUMN media_state TEXT NOT NULL DEFAULT 'unknown'
  CHECK (media_state IN ('unknown','empty','tray_open','not_ready','disc'));
ALTER TABLE drives ADD COLUMN disc_type TEXT
  CHECK (disc_type IS NULL OR disc_type IN ('audio','dvd','bluray','data','mixed','unknown'));
ALTER TABLE drives ADD COLUMN disc_label TEXT NOT NULL DEFAULT '';
ALTER TABLE drives ADD COLUMN media_changed_at TEXT;

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  drive_id TEXT NOT NULL REFERENCES drives(id) ON UPDATE CASCADE ON DELETE CASCADE,
  kind TEXT NOT NULL,
  state TEXT NOT NULL
    CHECK (state IN ('queued','waiting_space','running','done','failed','cancelled','interrupted')),
  params TEXT NOT NULL DEFAULT '{}',
  progress REAL NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  needs_bytes INTEGER NOT NULL DEFAULT 0,
  uses_drive INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  estimated_finish TEXT,
  error TEXT
);
CREATE INDEX jobs_drive_state ON jobs(drive_id, state, id);
