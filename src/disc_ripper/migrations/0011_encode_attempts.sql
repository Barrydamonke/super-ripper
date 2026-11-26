-- How many times an encode has been started: one that crashed the container twice is not re-queued again.
ALTER TABLE encodes ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
