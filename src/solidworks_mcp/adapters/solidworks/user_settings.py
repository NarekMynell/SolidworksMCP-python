"""The user's SolidWorks settings: off only while a call needs them, then back.

Some SolidWorks system options make an API call wait for a person, or change
what it draws: the Modify box on every new dimension, numeric input on new
sketch entities, the prompt when a dimension over-defines a sketch, and the
scaling of a whole sketch on its first dimension. Automation needs them off.
They are the user's own options, saved in their Windows profile, so
:class:`UserSettings` switches off the ones that are on when an outermost
adapter operation starts and puts exactly those back when it ends. Before
switching anything it writes a note naming what it changes; a server stopped
mid-call (its process killed) leaves the note, and the next connect puts
those options back (:meth:`UserSettings.recover`).
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from loguru import logger

#: ``swUserPreferenceToggle_e`` system options automation needs off, with where
#: a person finds them (Tools > Options > System Options; get_api_enum and the
#: API help's System Options page, SolidWorks 2025).
AUTOMATION_OFF: dict[int, str] = {
    10: "General > Input dimension value",  # its Modify box blocks AddDimension
    100: "Sketch > Over defining dimensions > Prompt to set driven state",
    372: "Sketch > Enable on screen numeric input on entity creation",
    520: "Sketch > Create dimension only when value is entered",
    642: "Sketch > Scale sketch on first dimension creation",  # rescales the sketch
}
#: ``GetExitCodeProcess``'s code for a process still running.
_STILL_ACTIVE = 259
#: ``PROCESS_QUERY_LIMITED_INFORMATION``: enough to read an exit code.
_QUERY_LIMITED = 0x1000


def notes_folder() -> Path:
    """Where the notes go: per Windows user, as the options are.

    ``SOLIDWORKS_MCP_USER_SETTINGS_DIR`` overrides it (the unit tests point it
    at a temporary folder, so a fake SolidWorks never meets a real note).

    Returns:
        Path: The folder; it may not exist yet.
    """
    override = os.environ.get("SOLIDWORKS_MCP_USER_SETTINGS_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".solidworks_mcp" / "user-settings"


def process_alive(pid: int) -> bool:
    """Whether a process with this id is running (Windows, as the adapter is).

    Args:
        pid: A process id.

    Returns:
        bool: False only when the process is known to have ended.
    """
    import pywintypes
    import win32api
    import win32process

    try:
        handle = win32api.OpenProcess(_QUERY_LIMITED, False, pid)
    except pywintypes.error as error:
        # 87 (invalid parameter): no such process. Denied access means it runs.
        return bool(error.winerror != 87)
    try:
        return bool(win32process.GetExitCodeProcess(handle) == _STILL_ACTIVE)
    finally:
        win32api.CloseHandle(handle)


class UserSettings:
    """Switch :data:`AUTOMATION_OFF` off around operations, and back after.

    Args:
        folder: Where the notes go (one ``<pid>.json`` per server process).
        alive: Tells whether a process still runs; :func:`process_alive`.
    """

    def __init__(
        self, folder: Path, alive: Callable[[int], bool] = process_alive
    ) -> None:
        """Keep the notes folder; nothing is read or written yet."""
        self._folder = folder
        self._alive = alive
        self._depth = 0
        #: What this process switched off and still owes back: toggle -> the
        #: user's value. Kept until SolidWorks takes it, so one put back that
        #: failed is tried again after the next operation.
        self._owed: dict[int, bool] = {}

    @property
    def note(self) -> Path:
        """This process's note: the options it switched off and must put back."""
        return self._folder / f"{os.getpid()}.json"

    @contextlib.contextmanager
    def automation(self, app: Any) -> Iterator[None]:
        """Run a block with the options off; a nested block shares the outer one.

        Args:
            app: The connected ``ISldWorks``, or None (nothing changes).

        Yields:
            None: While the options are off.
        """
        self._depth += 1
        try:
            if self._depth == 1 and app is not None:
                self._switch_off(app)
            yield
        finally:
            self._depth -= 1
            if self._depth == 0 and app is not None:
                self._put_back(app)

    def recover(self, app: Any) -> dict[int, bool]:
        """Put back what servers stopped mid-call left switched off.

        A note whose process still runs is left: that server puts its options
        back itself after its call. A note named after this process, while it
        owes nothing, is an ended process's whose id came back.

        Args:
            app: The connected ``ISldWorks``.

        Returns:
            dict[int, bool]: The options put back, with their values.
        """
        restored: dict[int, bool] = {}
        for note in sorted(self._folder.glob("*.json")):
            if not note.stem.isdigit():
                continue
            pid = int(note.stem)
            if pid == os.getpid():
                if self._depth or self._owed:
                    continue
            elif self._alive(pid):
                continue
            toggles = _read_note(note)
            failed = self._set_all(app, toggles)
            restored.update({t: v for t, v in toggles.items() if t not in failed})
            if failed:
                logger.warning("SolidWorks options not put back yet: {}", failed)
            else:
                with contextlib.suppress(OSError):
                    note.unlink()
        if restored:
            logger.info(
                "Put back SolidWorks options a stopped server left off: {}",
                ", ".join(AUTOMATION_OFF.get(t, str(t)) for t in restored),
            )
        return restored

    def _switch_off(self, app: Any) -> None:
        """Switch off the options that are on, after noting them."""
        on = {}
        for toggle in AUTOMATION_OFF:
            try:
                # COM answers a real bool; anything else (unreadable) is left
                # as it is, so it is never wrongly put back.
                if app.GetUserPreferenceToggle(toggle) is True:
                    on[toggle] = True
            except Exception:
                continue
        if not on and not self._owed:
            return
        # The note goes first: a server killed from here on leaves it behind.
        # Without one (a disk error) they are still switched off: a call held
        # by a Modify box is worse, and this process still puts them back.
        self._owed = {**self._owed, **on}
        self._write_note(self._owed)
        for toggle in on:
            with contextlib.suppress(Exception):
                app.SetUserPreferenceToggle(toggle, False)

    def _put_back(self, app: Any) -> None:
        """Put back what this process owes; the note goes once all of it is."""
        if not self._owed:
            return
        self._owed = self._set_all(app, self._owed)
        if self._owed:
            # SolidWorks stopped answering: tried again after the next
            # operation, or by the next server's connect.
            logger.warning("SolidWorks options not put back yet: {}", self._owed)
            self._write_note(self._owed)
        else:
            with contextlib.suppress(OSError):
                self.note.unlink()

    @staticmethod
    def _set_all(app: Any, toggles: dict[int, bool]) -> dict[int, bool]:
        """Set each toggle; the ones that failed."""
        failed = {}
        for toggle, value in toggles.items():
            try:
                app.SetUserPreferenceToggle(toggle, value)
            except Exception:
                failed[toggle] = value
        return failed

    def _write_note(self, toggles: dict[int, bool]) -> bool:
        """Write this process's note; whether it was written."""
        try:
            self._folder.mkdir(parents=True, exist_ok=True)
            temporary = self.note.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({str(t): v for t, v in toggles.items()}), encoding="utf-8"
            )
            os.replace(temporary, self.note)
        except OSError as error:
            logger.warning("Can't write the note of SolidWorks options: {}", error)
            return False
        return True


def _read_note(note: Path) -> dict[int, bool]:
    """A note's toggles; an unreadable note holds none."""
    try:
        data = json.loads(note.read_text(encoding="utf-8"))
        return {int(t): bool(v) for t, v in data.items() if int(t) in AUTOMATION_OFF}
    except (OSError, ValueError, AttributeError):
        return {}
