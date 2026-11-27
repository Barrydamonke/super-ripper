"""Undo an import (put the library back as it was) and skip a rip (give up on importing it)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from . import db, encodes, film_imports
from .clock import utc_now
from .filmimport import NO_RADARR, Flow, FlowError, _locked, wait_for_folder_move
from .radarr import RadarrError

BIN_GONE = "The old file is no longer in Radarr's recycle bin, so only the new file was taken back."
_UNDOABLE = ("imported", "in_jellyfin")


def _move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


def _remove_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:  # not empty (or already gone): leave it
        pass


async def undo(services, rip_id: int) -> str | None:
    """Take an imported film back out of the library. Returns the message ("Undone." ...) or why it can't."""
    client = services.radarr()
    if client is None:
        return NO_RADARR

    async def work():
        flow = Flow(services, client, rip_id)
        try:
            row = flow.row()
            if row is None or row["state"] not in _UNDOABLE:
                return "Only an imported film can be undone."
            flow.log.write("undo started")
            try:
                message = await _undo(flow, row)
            except _Refused as exc:
                flow.log.write(str(exc))
                return str(exc)
            except (RadarrError, FlowError, OSError) as exc:
                flow.log.write(f"UNDO FAILED: {exc}")
                return f"Undo stopped: {exc} Press Undo again to continue."
            flow.log.write(f"undone: {message}")
            return message
        finally:
            flow.close()

    return await _locked(services, rip_id, work)


class _Refused(Exception):
    """Undo can't go on; the message is shown as is and the row is left as it was."""


async def _undo(flow: Flow, row) -> str:
    enc = encodes.get(flow.conn, row["encode_id"])
    new_path = Path(row["new_path"]) if row["new_path"] else None
    old_folder = Path(row["old_folder"]) if row["old_folder"] else None
    old_path = Path(row["old_path"]) if row["old_path"] else None
    recycled = Path(row["recycled_path"]) if row["recycled_path"] else None
    replacing = old_path is not None
    message = "Undone."

    # Nothing is moved onto a file that is already there.
    if new_path is not None and enc is not None and await asyncio.to_thread(new_path.exists):
        if await asyncio.to_thread(Path(enc["output"]).exists):
            raise _Refused(f"Undo stopped: {enc['output']} already exists")
    if replacing and recycled is not None and await asyncio.to_thread(recycled.exists):
        if await asyncio.to_thread(old_path.exists) and old_path != new_path:  # new_path is moved away first
            raise _Refused(f"Undo stopped: {old_path} already exists")

    # Each step is recorded on the row as it finishes, so a retry carries on from where this stopped.
    extras = json.loads(row["extras"] or "[]")
    if extras:
        for extra in extras:
            await asyncio.to_thread(Path(extra).unlink, missing_ok=True)
        if new_path is not None:
            await asyncio.to_thread(_remove_empty, new_path.parent / "extras")
        flow.set(extras=[])

    folder = new_path.parent if new_path else None
    if replacing and old_folder is not None:
        movie = await flow.call("movie", row["movie_id"])
        shown = (movie.get("movieFile") or {}).get("path")
        settled = new_path is None or not shown or (
            Path(shown).is_relative_to(old_folder) and await asyncio.to_thread(Path(shown).exists))
        tidied = Path(movie["path"]) != old_folder  # the folder was tidied: put its old name back
        if tidied or not settled:  # (or an earlier try saved the path but Radarr hadn't moved the files yet)
            if tidied:
                await flow.call("update_movie", {**movie, "path": str(old_folder)}, move_files=True)
            if new_path is not None:  # recorded first: a timeout below leaves the row pointing at the right place
                new_path = old_folder / new_path.name
                folder = old_folder
                flow.set(new_path=str(new_path))
            await wait_for_folder_move(flow, row["movie_id"], str(old_folder))
            flow.log.write(f"folder restored: {old_folder}")

    if new_path is not None and enc is not None and await asyncio.to_thread(new_path.exists):
        await asyncio.to_thread(_move, new_path, Path(enc["output"]))
        flow.log.write(f"new file moved back to {enc['output']}")
    if new_path is not None:
        flow.set(new_path=None)

    if replacing:
        if recycled is not None and await asyncio.to_thread(recycled.exists):
            await asyncio.to_thread(_move, recycled, old_path)
            flow.log.write(f"old file restored to {old_path}")
        elif not await asyncio.to_thread(old_path.exists):
            message += " " + BIN_GONE
        await flow.call("command", "RescanMovie", movieId=row["movie_id"])
    elif row["added_by_us"]:
        try:
            folder = Path((await flow.call("movie", row["movie_id"]))["path"])
        except (RadarrError, KeyError):
            pass  # keep the folder the new file was in, if any
        await flow.call("delete_movie", row["movie_id"], delete_files=False)
        if folder is not None:
            await asyncio.to_thread(_remove_empty, folder / "extras")
            await asyncio.to_thread(_remove_empty, folder)
    elif row["movie_id"]:  # the owner already had this movie: leave it, and let Radarr see it has no file
        await flow.call("command", "RescanMovie", movieId=row["movie_id"])

    with flow.conn:
        flow.conn.execute("UPDATE encodes SET imported_path = NULL, imported_at = NULL WHERE rip_id = ?",
                          (flow.rip_id,))
    flow.set(state="undone", message=message)
    flow.set(state="needs_identify", step=film_imports.UNDONE_STEP, message=message, error=None, movie_id=None, encode_id=None,
             new_path=None, old_path=None, old_folder=None, recycled_path=None, extras=[], replace={},
             imported_at=None, added_by_us=0)
    return message


async def skip(services, rip_id: int) -> str | None:
    """Give up importing this rip: delete its downloads folder. Returns why it can't, or None."""
    async def work():
        conn = db.connect(services.config.db_path)
        try:
            row = film_imports.ensure(conn, rip_id, utc_now())
            if row["state"] == "importing":
                return "This rip is being imported; wait for it to finish."
            if row["state"] in _UNDOABLE:
                return "Use Undo first."
            if conn.execute("SELECT 1 FROM encodes WHERE rip_id = ? AND state IN ('queued', 'running')",
                            (rip_id,)).fetchone():
                return "An encode of this rip is still running; cancel it first."
            folder = services.config.downloads_dir / str(rip_id)
            try:
                if folder.exists():
                    await asyncio.to_thread(shutil.rmtree, folder)
            except OSError as exc:
                return f"Couldn't delete {folder}: {exc}"
            film_imports.update(conn, rip_id, utc_now(), state="skipped", message="Skipped.", error=None)
            return None
        finally:
            conn.close()

    return await _locked(services, rip_id, work)
