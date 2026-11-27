-- Whether the film was monitored in Radarr before the import (1/0; NULL: unknown), so Undo can restore it.
ALTER TABLE film_imports ADD COLUMN prev_monitored INTEGER;
