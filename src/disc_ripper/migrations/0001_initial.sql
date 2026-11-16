CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE drives (
  id TEXT PRIMARY KEY,
  id_source TEXT NOT NULL CHECK (id_source IN ('serial','wwid','port')),
  vendor TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', revision TEXT NOT NULL DEFAULT '',
  sg_name TEXT, sr_name TEXT,
  label TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1, present INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
