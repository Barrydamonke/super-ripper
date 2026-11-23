-- Encode presets become rows. Seeded ids are fixed: 1 Standard, 2 Fast, 3 Copy only.
-- The seed JSON below must equal presets.SEEDS (tests/test_migration_0008.py checks it).
CREATE TABLE presets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    settings TEXT NOT NULL,
    fingerprint TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX presets_name ON presets (lower(name));

INSERT INTO presets (id, name, settings, fingerprint, created_at, updated_at) VALUES
(1, 'Standard', '{"audio_channels":"max51","audio_codec":"eac3","audio_languages":["eng"],"audio_tracks":"first_per_language","bitrate_stereo":224,"bitrate_surround":640,"codec":"x265_10bit","crop":"off","deinterlace":"auto","engine":"handbrake","keep_commentary":false,"max_height":null,"mode":"software","quality_bluray":20,"quality_dvd":18,"speed":"slow","subtitle_languages":["eng"],"subtitles":"all"}', NULL, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
(2, 'Fast', '{"audio_channels":"max51","audio_codec":"eac3","audio_languages":["eng"],"audio_tracks":"first_per_language","bitrate_stereo":224,"bitrate_surround":640,"codec":"hevc_vaapi","crop":"off","deinterlace":"auto","engine":"ffmpeg","keep_commentary":false,"max_height":null,"mode":"hardware","quality_bluray":24,"quality_dvd":24,"speed":"slow","subtitle_languages":["eng"],"subtitles":"all"}', NULL, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
(3, 'Copy only', '{"audio_channels":"max51","audio_codec":"copy","audio_languages":["eng"],"audio_tracks":"all","bitrate_stereo":224,"bitrate_surround":640,"codec":"x265_10bit","crop":"off","deinterlace":"auto","engine":"ffmpeg","keep_commentary":false,"max_height":null,"mode":"copy","quality_bluray":20,"quality_dvd":18,"speed":"slow","subtitle_languages":["eng"],"subtitles":"all"}', NULL, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));

-- Fast keeps the quality the old fast_quality setting had.
UPDATE presets SET settings = json_set(settings,
    '$.quality_dvd', COALESCE((SELECT CAST(json_extract(value, '$') AS INTEGER) FROM settings WHERE key = 'fast_quality'), 24),
    '$.quality_bluray', COALESCE((SELECT CAST(json_extract(value, '$') AS INTEGER) FROM settings WHERE key = 'fast_quality'), 24))
WHERE id = 2;

-- Queued encodes get no snapshot: like any queued encode they follow their preset until they start.
-- encodes: preset TEXT becomes a reference plus a snapshot of what actually ran.
CREATE TABLE encodes_new (
  id INTEGER PRIMARY KEY,
  rip_id INTEGER NOT NULL,
  title INTEGER NOT NULL,
  role TEXT NOT NULL,
  source TEXT NOT NULL,
  disc_type TEXT NOT NULL,
  source_seconds REAL NOT NULL,
  source_bytes INTEGER NOT NULL,
  source_audio INTEGER NOT NULL DEFAULT -1,
  preset_id INTEGER REFERENCES presets(id) ON DELETE SET NULL,
  preset_name TEXT NOT NULL DEFAULT '',
  preset_settings TEXT,
  preset_fingerprint TEXT,
  kind TEXT NOT NULL DEFAULT 'film' CHECK (kind IN ('film','sample')),
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
  finished_at TEXT,
  imported_path TEXT,
  imported_at TEXT
);

INSERT INTO encodes_new (id, rip_id, title, role, source, disc_type, source_seconds, source_bytes, source_audio,
                         preset_id, preset_name, preset_settings, state, progress, message, error,
                         estimated_finish, output, output_bytes, speed, created_at, started_at, finished_at,
                         imported_path, imported_at)
SELECT e.id, e.rip_id, e.title, e.role, e.source, e.disc_type, e.source_seconds, e.source_bytes, e.source_audio,
       p.id, p.name, CASE WHEN e.state = 'queued' THEN NULL ELSE p.settings END, e.state, e.progress, e.message, e.error,
       e.estimated_finish, e.output, e.output_bytes, e.speed, e.created_at, e.started_at, e.finished_at,
       e.imported_path, e.imported_at
FROM encodes e
JOIN presets p ON p.id = CASE e.preset WHEN 'standard' THEN 1 WHEN 'fast' THEN 2 ELSE 3 END;

DROP TABLE encodes;
ALTER TABLE encodes_new RENAME TO encodes;
CREATE INDEX encodes_state ON encodes (state, id);
CREATE INDEX encodes_rip_title ON encodes (rip_id, title);
CREATE UNIQUE INDEX encodes_active_file ON encodes (rip_id, title) WHERE state IN ('queued','running');

-- Choices are preset ids as decimal strings, or 'off'.
UPDATE drives SET encode_choice = CASE encode_choice
    WHEN 'standard' THEN '1' WHEN 'fast' THEN '2' WHEN 'copy' THEN '3' WHEN 'off' THEN 'off' END
WHERE encode_choice IS NOT NULL;
UPDATE video_rips SET encode_preset = CASE encode_preset
    WHEN 'standard' THEN '1' WHEN 'fast' THEN '2' WHEN 'copy' THEN '3' WHEN 'off' THEN 'off' END
WHERE encode_preset IS NOT NULL;

-- The old setting becomes the after-rip default; the old settings keys go.
INSERT INTO settings (key, value)
SELECT 'internal.encode_default', json_quote(CASE json_extract(value, '$')
    WHEN 'fast' THEN '2' WHEN 'copy' THEN '3' WHEN 'off' THEN 'off' ELSE '1' END)
FROM settings WHERE key = 'encode_after_rip'
ON CONFLICT(key) DO UPDATE SET value = excluded.value;
DELETE FROM settings WHERE key IN ('encode_after_rip', 'fast_quality');

-- MakeMKV's track selection: replaced only where it still has the old default.
UPDATE settings SET value = json_quote('-sel:all,+sel:video,+sel:audio,+sel:subtitle,-sel:mvcvideo,=100:all,-10:eng')
WHERE key = 'video_track_selection' AND value = json_quote('-sel:all,+sel:video,+sel:(eng|nolang),+sel:(audio&single),-sel:(audio&special),+sel:(subtitle&forced),-sel:mvcvideo,=100:all,-10:eng');
