CREATE TABLE encodes (
  id INTEGER PRIMARY KEY,
  rip_id INTEGER NOT NULL,
  title INTEGER NOT NULL,
  role TEXT NOT NULL,
  source TEXT NOT NULL,
  disc_type TEXT NOT NULL,
  source_seconds REAL NOT NULL,
  source_bytes INTEGER NOT NULL,
  source_audio INTEGER NOT NULL DEFAULT -1,
  preset TEXT NOT NULL CHECK (preset IN ('standard','fast','copy')),
  state TEXT NOT NULL CHECK (state IN ('queued','running','done','failed','cancelled','interrupted')),
  progress REAL NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  error TEXT,
  estimated_finish TEXT,
  output TEXT,
  output_bytes INTEGER,
  speed REAL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);

CREATE INDEX encodes_state ON encodes (state, id);
CREATE INDEX encodes_rip_title ON encodes (rip_id, title);
CREATE UNIQUE INDEX encodes_active_file ON encodes (rip_id, title) WHERE state IN ('queued','running');
