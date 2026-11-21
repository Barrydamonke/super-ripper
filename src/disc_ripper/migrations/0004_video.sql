ALTER TABLE drives ADD COLUMN video_hold INTEGER NOT NULL DEFAULT 0;

CREATE TABLE video_discs (
  fingerprint TEXT PRIMARY KEY,
  disc_type TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  scan TEXT NOT NULL DEFAULT '{}',
  minlength INTEGER,
  held TEXT NOT NULL DEFAULT '[]',
  as_film INTEGER NOT NULL DEFAULT 0,
  drive_id TEXT,
  last_seen TEXT NOT NULL
);

CREATE TABLE video_rips (
  rip_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  fingerprint TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('ripping','ripped','move_failed','failed')),
  main_title INTEGER,
  files TEXT NOT NULL DEFAULT '{}',
  message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX video_rips_fingerprint ON video_rips(fingerprint, state);
