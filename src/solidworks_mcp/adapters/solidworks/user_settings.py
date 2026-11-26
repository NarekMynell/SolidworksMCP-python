"""The user's SolidWorks options: off only while a call needs them, then back.

Some SolidWorks system options make an API call wait for a person, or change
what it draws: the Modify box on every new dimension, numeric input on new
sketch entities, the prompt when a dimension over-defines a sketch, the
scaling of a whole sketch on its first dimension, the sheet format dialog of
a new drawing. Automation needs them off. They are the user's own options,
saved in their Windows profile, so they are off only while some server's call
runs, and back as the user had them after.

Several servers (one per Claude Code session) can drive one SolidWorks. They
share a **ledger** per SolidWorks release in the notes folder: the user's
values of the options switched off (``owed``) and the adapters inside a call
(``holders``). The first one in switches the options off; the last one out
puts them back, so no server restores them under another's call. A named
mutex guards only that bookkeeping, a few COM calls, never a whole call, so
one session's long call or open dialog never holds up another's. A server
killed mid-call stays a holder until its process has ended; then the next
call or connect of any server drops it, and puts the options back if no
holder is left.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from loguru import logger

#: ``swUserPreferenceToggle_e`` system options automation needs off, with where
#: a person finds them (Tools > Options > System Options; get_api_enum and the
#: API help's System Options pages, SolidWorks 2025).
AUTOMATION_OFF: dict[int, str] = {
    10: "General > Input dimension value",  # its Modify box blocks AddDimension
    100: "Sketch > Over defining dimensions > Prompt to set driven state",
    353: "Drawings > Show sheet format dialog when adding new sheet",  # pitfall #32
    372: "Sketch > Enable on screen numeric input on entity creation",
    520: "Sketch > Create dimension only when value is entered",
    # Off as upstream had it; left on, an API dimension rescaled nothing (live control).
    642: "Sketch > Scale sketch on first dimension creation",
}
#: How long the bookkeeping waits for another server's (a few COM calls; long
#: only while SolidWorks itself is busy with a call).
LOCK_SECONDS = 120.0
#: A connect waits less: its repair is also done by the next call.
CONNECT_LOCK_SECONDS = 5.0
#: ``GetExitCodeProcess``'s code for a process still running.
_STILL_ACTIVE = 259
#: ``PROCESS_QUERY_LIMITED_INFORMATION``: enough for exit code and times.
_QUERY_LIMITED = 0x1000
#: Two start times this close (seconds) are one process's.
_SAME_START = 0.01


def notes_folder() -> Path:
    """Where the ledgers go: per Windows user, as the options are.

    ``SOLIDWORKS_MCP_USER_SETTINGS_DIR`` overrides it (the unit tests point it
    at a temporary folder, so a fake SolidWorks never meets a real ledger).

    Returns:
        Path: The folder; it may not exist yet.
    """
    override = os.environ.get("SOLIDWORKS_MCP_USER_SETTINGS_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".solidworks_mcp" / "user-settings"


def process_started(pid: int | None = None) -> float | None:
    """When a running process started, seconds since the epoch.

    Args:
        pid: A process id; this process when None.

    Returns:
        float | None: None when the process has ended or never was; NaN when
        it runs but its start can't be read (access denied).
    """
    import pywintypes
    import win32api
    import win32process

    if pid is None:
        times = win32process.GetProcessTimes(win32api.GetCurrentProcess())
        return float(times["CreationTime"].timestamp())
    try:
        handle = win32api.OpenProcess(_QUERY_LIMITED, False, pid)
    except pywintypes.error as error:
        # 87 (invalid parameter): no such process. Denied access means it runs.
        return None if error.winerror == 87 else math.nan
    try:
        if win32process.GetExitCodeProcess(handle) != _STILL_ACTIVE:
            return None
        return float(win32process.GetProcessTimes(handle)["CreationTime"].timestamp())
    except pywintypes.error:
        return math.nan
    finally:
        win32api.CloseHandle(handle)


def process_alive(pid: int, started: float | None) -> bool:
    """Whether the process a holder or note names still runs.

    Args:
        pid: Its process id.
        started: When that process started, if known; a process with the same
            id that started at another time took a reused id.

    Returns:
        bool: False only when that process is known to have ended.
    """
    now = process_started(pid)
    if now is None:
        return False
    if started is None or math.isnan(now):
        return True
    return abs(now - started) <= _SAME_START


class Lock:
    """The bookkeeping lock of the servers that share a notes folder: a named mutex.

    Windows lets a mutex go when the process holding it ends, and the next
    waiter is told it was abandoned. A mutex belongs to the thread that took
    it: :meth:`held` takes and gives it back in one call, on one thread.

    Args:
        name: The mutex's name (``Local\\...``: this logon session).
    """

    def __init__(self, name: str) -> None:
        """Keep the name; the mutex is opened on first use."""
        self.name = name
        self._handle: Any = None

    @contextlib.contextmanager
    def held(self, seconds: float) -> Iterator[bool]:
        """Hold the lock for a block; yields whether it was taken in time.

        A lock that can't be made or taken never fails a call: the block runs
        without it (False).
        """
        taken = False
        try:
            import win32event

            if self._handle is None:
                self._handle = win32event.CreateMutex(None, False, self.name)
            waited = win32event.WaitForSingleObject(self._handle, int(seconds * 1000))
            taken = waited in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED)
        except Exception as error:
            logger.warning("The SolidWorks options lock failed: {}", error)
        try:
            yield taken
        finally:
            if taken:
                with contextlib.suppress(Exception):
                    win32event.ReleaseMutex(self._handle)


def lock_for(folder: Path) -> Lock:
    """The lock of the servers that share ``folder``.

    Args:
        folder: The notes folder.

    Returns:
        Lock: A mutex named after the folder, so tests with their own folder
        never wait for a real server.
    """
    digest = hashlib.sha1(str(folder).lower().encode("utf-8")).hexdigest()[:16]
    return Lock(f"Local\\solidworks-mcp-{digest}")


class UserSettings:
    """Switch :data:`AUTOMATION_OFF` off around operations, and back after.

    Args:
        folder: Where the ledgers go (``ledger-<release>.json``).
        alive: Tells whether a holder's process still runs; :func:`process_alive`.
        lock: The cross-server lock; :func:`lock_for` of ``folder``.
        started: When this process started; read when None.
    """

    def __init__(
        self,
        folder: Path,
        alive: Callable[[int, float | None], bool] = process_alive,
        lock: Lock | None = None,
        started: float | None = None,
    ) -> None:
        """Keep the notes folder; nothing is read or written yet."""
        self._folder = folder
        self._alive = alive
        self._lock = lock if lock is not None else lock_for(folder)
        self._started = started
        self.id = secrets.token_hex(4)
        self._depth = 0
        self._inside = False
        #: What this adapter's call switched off, and whether the ledger knows it.
        self._mine: dict[int, bool] = {}
        self._persisted = True
        #: Each option's value at the start of this adapter's call (None: unread).
        self._known: dict[int, bool | None] = {}
        #: (id of the app, its major version): never the app itself, which must not
        #: outlive the connection (released after CoUninitialize it breaks the next
        #: connections in the process: RPC_S_UNKNOWN_IF, pitfall 48).
        self._release: tuple[int, str | None] | None = None
        self._warned: set[str] = set()

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
                self._enter(app)
            yield
        finally:
            self._depth -= 1
            if self._depth == 0 and app is not None:
                self._leave(app)

    def keep_off(self, app: Any, toggles: tuple[int, ...]) -> None:
        """Switch options off again inside a call (helpers that open sketches).

        Only those read at the call's start: an option this call couldn't read
        is never switched, so it can't be left off.

        Args:
            app: The connected ``ISldWorks``.
            toggles: ``AUTOMATION_OFF`` keys.
        """
        for toggle in toggles:
            if self._known.get(toggle) is None:
                continue
            try:
                app.SetUserPreferenceToggle(toggle, False)
            except Exception as error:
                self._warn(
                    f"off {toggle}",
                    f"Can't switch {AUTOMATION_OFF[toggle]} off: {error}",
                )

    def recover(self, app: Any) -> dict[int, bool]:
        """At connect: drop holders that ended, and put the options back if none is left.

        Args:
            app: The connected ``ISldWorks``.

        Returns:
            dict[int, bool]: The options put back, with their values.
        """
        if self._depth:
            return {}
        restored: dict[int, bool] = {}
        with self._lock.held(CONNECT_LOCK_SECONDS) as taken:
            if not taken:
                self._warn(
                    "connect",
                    "Another server holds the options lock; the next call repairs them",
                )
                return {}
            ledger = self._read(app)
            restored = self._settle(app, ledger)
            self._write(app, ledger)
        return restored

    # ------------------------------------------------------------------ calls

    def _enter(self, app: Any) -> None:
        """Join the holders; switch off the options that are on, noting the user's values."""
        self._known = {}
        self._mine = {}
        with self._lock.held(LOCK_SECONDS) as taken:
            if not taken:
                self._warn(
                    "lock",
                    f"The options lock stayed taken {LOCK_SECONDS:g} s; switching without it",
                )
            ledger = self._read(app)
            self._settle(app, ledger)
            for toggle, where in AUTOMATION_OFF.items():
                try:
                    value = app.GetUserPreferenceToggle(toggle)
                except Exception as error:
                    self._warn(
                        f"read {toggle}", f"Can't read {where}: {error}; left as it is"
                    )
                    self._known[toggle] = None
                    continue
                # COM answers a real bool; anything else is left alone, so it
                # is never wrongly put back.
                self._known[toggle] = value if isinstance(value, bool) else None
                if value is not True:
                    continue
                ledger["owed"].setdefault(
                    toggle, True
                )  # the user's value, first seen on
                self._mine[toggle] = True
                try:
                    app.SetUserPreferenceToggle(toggle, False)
                except Exception as error:
                    self._warn(f"off {toggle}", f"Can't switch {where} off: {error}")
            ledger["holders"].append(self._holder())
            self._inside = True
            self._persisted = self._write(app, ledger)

    def _leave(self, app: Any) -> None:
        """Leave the holders; the last one out puts the options back."""
        if not self._inside:
            return
        self._inside = False
        with self._lock.held(LOCK_SECONDS) as taken:
            if not taken:
                self._warn(
                    "lock",
                    f"The options lock stayed taken {LOCK_SECONDS:g} s; putting back without it",
                )
            ledger = self._read(app)
            if not self._persisted:
                # The ledger on disk never heard of this call: what it switched off is owed.
                for toggle, value in self._mine.items():
                    ledger["owed"].setdefault(toggle, value)
            ledger["holders"] = [h for h in ledger["holders"] if h.get("id") != self.id]
            self._settle(app, ledger)
            self._write(app, ledger)

    def _settle(self, app: Any, ledger: dict[str, Any]) -> dict[int, bool]:
        """Drop holders that ended; with none left, put back what is owed."""
        ledger["holders"] = [h for h in ledger["holders"] if self._running(h)]
        if ledger["holders"] or not ledger["owed"]:
            return {}
        owed = dict(ledger["owed"])
        failed = self._set_all(app, owed)
        ledger["owed"] = failed
        if failed:
            logger.warning("SolidWorks options not put back yet: {}", failed)
        return {t: v for t, v in owed.items() if t not in failed}

    def _running(self, holder: dict[str, Any]) -> bool:
        """Whether a holder's process still runs (one of this process: by its start)."""
        pid, started = holder.get("pid"), holder.get("started")
        if not isinstance(pid, int):
            return False
        if pid == os.getpid():
            if holder.get("id") == self.id:
                return (
                    self._depth > 0 and self._inside
                )  # this adapter, still in its call
            return started is None or abs(float(started) - self._start()) <= _SAME_START
        return bool(
            self._alive(
                pid, float(started) if isinstance(started, int | float) else None
            )
        )

    # ------------------------------------------------------------------ the ledger

    def _path(self, app: Any) -> Path:
        """This release's ledger: options are SolidWorks' per release."""
        return self._folder / f"ledger-{self._version(app) or 'unknown'}.json"

    def _read(self, app: Any) -> dict[str, Any]:
        """The ledger, with old notes of ended servers merged in (an empty one if none)."""
        ledger: dict[str, Any] = {"owed": {}, "holders": []}
        path = self._path(app)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ledger["owed"] = {
                int(t): v
                for t, v in dict(data.get("owed") or {}).items()
                if str(t).isdigit() and int(t) in AUTOMATION_OFF and isinstance(v, bool)
            }
            ledger["holders"] = [
                h for h in list(data.get("holders") or []) if isinstance(h, dict)
            ]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as error:
            self._warn(
                "ledger", f"Ignoring an unreadable options ledger {path.name}: {error}"
            )
        self._merge_old_notes(ledger)
        return ledger

    def _merge_old_notes(self, ledger: dict[str, Any]) -> None:
        """Notes of the first release (``<pid>.json``: toggles only), once their server has ended."""
        for note in sorted(self._folder.glob("*.json")):
            if not note.stem.isdigit() or self._alive(int(note.stem), None):
                continue
            try:
                data = json.loads(note.read_text(encoding="utf-8"))
                toggles = {
                    int(t): v
                    for t, v in dict(data).items()
                    if str(t).isdigit() and isinstance(v, bool)
                }
            except (OSError, ValueError, TypeError):
                toggles = {}
            for toggle, value in toggles.items():
                if toggle in AUTOMATION_OFF:
                    ledger["owed"].setdefault(toggle, value)
            with contextlib.suppress(OSError):
                note.unlink()

    def _write(self, app: Any, ledger: dict[str, Any]) -> bool:
        """Write the ledger (removed when it holds nothing); whether it was."""
        path = self._path(app)
        try:
            if not ledger["owed"] and not ledger["holders"]:
                path.unlink(missing_ok=True)
                return True
            self._folder.mkdir(parents=True, exist_ok=True)
            document = {
                "owed": {str(t): v for t, v in ledger["owed"].items()},
                "holders": ledger["holders"],
            }
            temporary = path.with_suffix(f".{self.id}.tmp")
            temporary.write_text(json.dumps(document), encoding="utf-8")
            os.replace(temporary, path)
        except OSError as error:
            self._warn("write", f"Can't write the options ledger: {error}")
            return False
        return True

    def _holder(self) -> dict[str, Any]:
        return {"pid": os.getpid(), "started": self._start(), "id": self.id}

    def _start(self) -> float:
        """When this process started."""
        if self._started is None:
            self._started = process_started()
        return float(self._started or 0.0)

    def _version(self, app: Any) -> str | None:
        """SolidWorks' major version (``33`` for 2025), read once per connection."""
        if self._release is None or self._release[0] != id(app):
            self._release = (id(app), _version(app))
        return self._release[1]

    def forget(self) -> None:
        """Forget the connection (on disconnect): a next app may reuse the old one's id."""
        self._release = None

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

    def _warn(self, key: str, message: str) -> None:
        """Log a problem once per adapter (each operation would repeat it)."""
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message)


def keep_off(adapter: Any, toggles: tuple[int, ...]) -> None:
    """Switch options off again inside the adapter's call (a helper that opens sketches).

    Args:
        adapter: The connected adapter; one without :class:`UserSettings`
            switched nothing, so nothing is switched.
        toggles: ``AUTOMATION_OFF`` keys.
    """
    settings = getattr(adapter, "_user_settings", None)
    app = getattr(adapter, "swApp", None)
    if isinstance(settings, UserSettings) and app is not None:
        settings.keep_off(app, toggles)


def _version(app: Any) -> str | None:
    """SolidWorks' major version (``33`` for 2025), whose options a ledger holds."""
    try:
        raw = app.RevisionNumber
        raw = raw() if callable(raw) else raw
    except Exception:
        return None
    text = str(raw).split(".")[0].strip()
    return text if text.isdigit() else None
