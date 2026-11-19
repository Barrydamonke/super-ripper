"""Move tagged files into the music library and remember where each album lives.

The albums table links a release (and other pressings that joined it) to its
folder, so a later disc of a set lands in the same folder whatever its tags say.
Nothing in the library is ever overwritten.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import library, tagging


class FileExists(Exception):
    """A target file or folder is already in the library."""


@dataclass
class AlbumPlan:
    album_key: str
    release_id: str | None
    release_group_id: str | None
    album_artist: str
    album: str
    year: str
    compilation: bool
    disc: int | None  # None: single-disc release (no "1-" prefix)
    disc_total: int
    disc_id: str
    album_tags: dict[str, str]
    tracks: list[tuple[Path, dict[str, str]]] = field(default_factory=list)  # source file -> full tags


def _album_row(conn: sqlite3.Connection, plan: AlbumPlan) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM albums WHERE album_key = ?", (plan.album_key,)).fetchone()
    if row is None and plan.release_id:
        row = conn.execute(
            "SELECT albums.* FROM albums JOIN album_releases USING (album_key) WHERE release_id = ?",
            (plan.release_id,),
        ).fetchone()
    return row


def _new_folder(conn: sqlite3.Connection, plan: AlbumPlan, music_dir: Path, allow_duplicate_folder: bool) -> PurePosixPath:
    rel = library.album_folder(plan.album_artist, plan.album, plan.year, compilation=plan.compilation)
    candidates = [rel]
    if allow_duplicate_folder:
        candidates += [rel.with_name(f"{rel.name} ({n})") for n in range(2, 100)]
    for candidate in candidates:
        taken = (music_dir / candidate).exists() or conn.execute(
            "SELECT 1 FROM albums WHERE folder = ?", (str(candidate),)
        ).fetchone()
        if not taken:
            return candidate
    raise FileExists(f"{rel} is already in the library.")


def _target_name(plan: AlbumPlan, tags: dict[str, str]) -> str:
    track_artist = tags.get("ARTIST") if plan.compilation else None
    return library.track_filename(int(tags["TRACKNUMBER"]), tags.get("TITLE", ""), disc=plan.disc,
                                  track_artist=track_artist)


def _move(src: Path, dst: Path) -> None:
    try:
        os.replace(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copy2(src, dst)  # across mounts (e.g. /work → /data)
        src.unlink()


def file_album(
    conn: sqlite3.Connection,
    plan: AlbumPlan,
    music_dir: Path,
    *,
    cover: Path | None,
    now: str,
    allow_duplicate_folder: bool = False,
) -> Path:
    row = _album_row(conn, plan)
    if row is not None and not allow_duplicate_folder:
        rel = PurePosixPath(row["folder"])
        album_key = row["album_key"]
    else:
        rel = _new_folder(conn, plan, music_dir, allow_duplicate_folder)
        album_key = plan.album_key if row is None else f"{plan.album_key}#{rel.name}"
    folder = music_dir / rel

    targets = [(src, tags, folder / _target_name(plan, tags)) for src, tags in plan.tracks]
    for _src, _tags, dst in targets:  # check everything before moving anything
        if dst.exists():
            raise FileExists(f"{dst.relative_to(music_dir)} is already in the library.")
    folder.mkdir(parents=True, exist_ok=True)
    for src, tags, dst in targets:
        tagging.write_tags(src, tags)
        _move(src, dst)
    if cover is not None and cover.is_file() and not (folder / "cover.jpg").exists():
        shutil.copy2(cover, folder / "cover.jpg")

    disc_number = str(plan.disc or 1)
    with conn:
        existing = conn.execute("SELECT discs FROM albums WHERE album_key = ?", (album_key,)).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO albums (album_key, folder, release_group_id, album_artist, album, year, disc_total,
                                    album_tags, discs, filed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (album_key, str(rel), plan.release_group_id, plan.album_artist, plan.album, plan.year,
                 plan.disc_total, json.dumps(plan.album_tags), json.dumps({disc_number: plan.disc_id}), now),
            )
        else:
            discs = json.loads(existing["discs"])
            discs[disc_number] = plan.disc_id
            conn.execute("UPDATE albums SET discs = ? WHERE album_key = ?", (json.dumps(discs), album_key))
        if plan.release_id:
            conn.execute(
                "INSERT OR IGNORE INTO album_releases (release_id, album_key) VALUES (?, ?)",
                (plan.release_id, album_key),
            )
    return folder


def retag_album(
    conn: sqlite3.Connection, album_key: str, new_album_tags: dict[str, str], music_dir: Path, now: str
) -> Path:
    """Change album-level tags on every file; move the folder if its name changes."""
    row = conn.execute("SELECT * FROM albums WHERE album_key = ?", (album_key,)).fetchone()
    if row is None:
        raise KeyError(album_key)
    tags = {**json.loads(row["album_tags"]), **new_album_tags}
    album_artist = tags.get("ALBUMARTIST", row["album_artist"])
    album = tags.get("ALBUM", row["album"])
    year = (tags.get("DATE") or "")[:4]
    compilation = PurePosixPath(row["folder"]).parts[0] == library.VARIOUS_ARTISTS
    old_rel = PurePosixPath(row["folder"])
    new_rel = library.album_folder(album_artist, album, year, compilation=compilation)
    if new_rel != old_rel and (music_dir / new_rel).exists():
        raise FileExists(f"{new_rel} is already in the library.")

    old_folder = music_dir / old_rel
    for path in sorted(old_folder.glob("*.flac")):
        tagging.update_tags(path, new_album_tags)
    folder = old_folder
    if new_rel != old_rel:
        folder = music_dir / new_rel
        folder.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old_folder, folder)
        try:
            old_folder.parent.rmdir()  # the old artist folder, if it is now empty
        except OSError:
            pass
    with conn:
        conn.execute(
            "UPDATE albums SET folder = ?, album_artist = ?, album = ?, year = ?, album_tags = ? WHERE album_key = ?",
            (str(new_rel), album_artist, album, year, json.dumps(tags), album_key),
        )
    return folder
