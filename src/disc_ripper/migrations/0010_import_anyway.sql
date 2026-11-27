-- The owner pressed "Import anyway" on a rip with a failed title: import without waiting for it.
ALTER TABLE video_rips ADD COLUMN import_anyway INTEGER NOT NULL DEFAULT 0;
