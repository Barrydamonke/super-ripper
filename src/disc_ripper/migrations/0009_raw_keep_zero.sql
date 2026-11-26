-- Raw rips are deleted as soon as an encode of them passes its check (the owner's rule: they can be
-- ripped again). A saved value equal to the old default (24 hours) is moved to the new default, 0.
UPDATE settings SET value = '0' WHERE key = 'raw_keep_hours' AND value = '24';
