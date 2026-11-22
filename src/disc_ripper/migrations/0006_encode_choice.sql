-- The encode preset chosen for the disc in a drive (NULL: the encode_after_rip setting),
-- copied onto the rip when it starts; video_rips.encode_preset is the rip's own choice.
ALTER TABLE drives ADD COLUMN encode_choice TEXT;
ALTER TABLE video_rips ADD COLUMN encode_preset TEXT;
