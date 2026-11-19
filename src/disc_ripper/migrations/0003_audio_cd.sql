ALTER TABLE drives ADD COLUMN read_offset INTEGER NOT NULL DEFAULT 0;
-- AccurateRip drive database values for the owner's drives (1,236 and 1,556
-- agreeing submissions). The iHAS124 W and Y variants use +48 and stay 0.
UPDATE drives SET read_offset = 667 WHERE model LIKE '%BH10LS38%';
UPDATE drives SET read_offset = 6
  WHERE model LIKE '%iHAS124%' AND model NOT LIKE '%iHAS124%W%' AND model NOT LIKE '%iHAS124%Y%';

CREATE TABLE discs (
  disc_id TEXT PRIMARY KEY,
  toc TEXT NOT NULL,
  track_count INTEGER NOT NULL,
  drive_id TEXT,
  last_seen TEXT NOT NULL
);

CREATE TABLE cd_rips (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  disc_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('ripping','needs_look','filed','discarded')),
  reasons TEXT NOT NULL DEFAULT '[]',
  candidates TEXT NOT NULL DEFAULT '[]',
  release_id TEXT,
  medium INTEGER,
  accuraterip TEXT NOT NULL DEFAULT '[]',
  downloads_path TEXT,
  filed_path TEXT,
  album_key TEXT,
  summary TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE INDEX cd_rips_disc ON cd_rips(disc_id, state);

CREATE TABLE albums (
  album_key TEXT PRIMARY KEY,
  folder TEXT NOT NULL UNIQUE,
  release_group_id TEXT,
  album_artist TEXT NOT NULL,
  album TEXT NOT NULL,
  year TEXT NOT NULL DEFAULT '',
  disc_total INTEGER NOT NULL DEFAULT 1,
  album_tags TEXT NOT NULL DEFAULT '{}',
  discs TEXT NOT NULL DEFAULT '{}',
  filed_at TEXT NOT NULL
);

CREATE TABLE album_releases (
  release_id TEXT PRIMARY KEY,
  album_key TEXT NOT NULL REFERENCES albums(album_key) ON UPDATE CASCADE
);
