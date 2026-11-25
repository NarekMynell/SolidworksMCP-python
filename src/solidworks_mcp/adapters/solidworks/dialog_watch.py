"""Answer a dialog SolidWorks left waiting for the user while a call runs (issue #97).

A COM call doesn't return while SolidWorks waits on a dialog (a macro's
``MsgBox`` or form, a SolidWorks prompt), and COM calls can't move off the
connect thread to time it out. :class:`DialogWatch` runs beside the call on
its own thread and uses only Win32 window calls, never COM. When a dialog the
SolidWorks process opened stays up longer than the timeout, it reads the
dialog and answers it:

- a dialog in ``KNOWN_ANSWERS`` gets its safe answer: SolidWorks' warning
  that memory is critically low recommends not to continue, so **No** (it
  hung a headless run for 30 minutes, seen live);
- with ``only_known`` (every server call) any other dialog is left alone and
  logged: a progress window may be one;
- otherwise (a macro) the non-committal choice: **Cancel** when the dialog
  has it, else its **only button** (an OK acknowledgement), else, with **no
  buttons** (a VBA user form), it closes the window. A dialog with several
  buttons and no Cancel (Yes/No) is left for the user and logged: pressing
  Yes or No could change the model.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol

from loguru import logger

#: Window classes of a message box or common dialog, and of a VBA user form.
DIALOG_CLASSES = frozenset({"#32770", "ThunderDFrame"})
#: Controls holding a dialog's message: a message box's, a task dialog's.
TEXT_CLASSES = frozenset({"Static", "SysLink"})
#: A dialog whose message holds the text gets the answer: the button labelled so.
KNOWN_ANSWERS: tuple[tuple[str, str], ...] = (("critically low on memory", "No"),)
_WM_CLOSE = 0x0010
_WM_COMMAND = 0x0111
_BM_CLICK = 0x00F5
#: ``TDM_CLICK_BUTTON`` (WM_USER + 102): a task dialog clicks its own button,
#: active window or not; the common buttons' IDs by label.
_TDM_CLICK_BUTTON = 0x0466
_COMMON_IDS = {"ok": 1, "cancel": 2, "yes": 6, "no": 7}
#: Presses of one button at most, a poll apart, while its dialog stays open.
PRESSES = 5
_LINK_TAG = re.compile(r"<[^>]*>")


@dataclass(frozen=True)
class DialogSeen:
    """A dialog the watch found waiting, and what it did.

    Attributes:
        title: The dialog's title.
        text: Its message, the static texts joined.
        buttons: Its button labels, without ``&``.
        answer: The label of the button pressed, ``"closed"``, or None when
            it was left for the user (or the press didn't go through).
        opened_before: It was already open when the watched call started, so
            not that call's doing.
    """

    title: str
    text: str
    buttons: tuple[str, ...]
    answer: str | None
    opened_before: bool = False

    def describe(self) -> str:
        """One line: the dialog, its text, and how it was answered."""
        said = f": {self.text!r}" if self.text else ""
        if self.answer is None:
            how = "left open for the user"
        elif self.answer == "closed":
            how = "closed"
        else:
            how = f"answered {self.answer!r}"
        return f"{self.title!r}{said} ({how})"


class Windows(Protocol):
    """The Win32 window calls the watch makes."""

    def dialogs(self, pid: int) -> list[int]:
        """Visible top-level dialog windows of process ``pid``."""

    def title(self, hwnd: int) -> str:
        """A window's title."""

    def children(self, hwnd: int) -> list[tuple[int, str, str]]:
        """A window's children as ``(hwnd, class name, text)``."""

    def control_id(self, hwnd: int) -> int:
        """A child control's ID."""

    def enabled(self, hwnd: int) -> bool:
        """Whether a window takes input (a dialog showing its own prompt doesn't)."""

    def post(self, hwnd: int, message: int, wparam: int, lparam: int) -> None:
        """Post a message to a window without waiting for it."""


class Win32Windows:
    """:class:`Windows` over ``win32gui`` and ``win32process``.

    Args:
        gui: The ``win32gui`` module; imported when None.
        process: The ``win32process`` module; imported when None.
    """

    def __init__(self, gui: Any = None, process: Any = None) -> None:
        """Keep the two modules."""
        if gui is None or process is None:
            import win32gui
            import win32process

            gui, process = gui or win32gui, process or win32process
        self._gui = gui
        self._process = process

    def dialogs(self, pid: int) -> list[int]:
        """Visible top-level dialog windows of process ``pid``."""
        found: list[int] = []

        def _visit(hwnd: int, _extra: Any) -> bool:
            if (
                self._gui.IsWindowVisible(hwnd)
                and self._gui.GetClassName(hwnd) in DIALOG_CLASSES
                and self._process.GetWindowThreadProcessId(hwnd)[1] == pid
            ):
                found.append(hwnd)
            return True

        self._gui.EnumWindows(_visit, None)
        return found

    def title(self, hwnd: int) -> str:
        """A window's title."""
        return str(self._gui.GetWindowText(hwnd))

    def children(self, hwnd: int) -> list[tuple[int, str, str]]:
        """A window's children as ``(hwnd, class name, text)``."""
        kids: list[tuple[int, str, str]] = []

        def _visit(child: int, _extra: Any) -> bool:
            kids.append(
                (child, self._gui.GetClassName(child), self._gui.GetWindowText(child))
            )
            return True

        self._gui.EnumChildWindows(hwnd, _visit, None)
        return kids

    def control_id(self, hwnd: int) -> int:
        """A child control's ID."""
        return int(self._gui.GetDlgCtrlID(hwnd))

    def enabled(self, hwnd: int) -> bool:
        """Whether a window takes input (a dialog showing its own prompt doesn't)."""
        return bool(self._gui.IsWindowEnabled(hwnd))

    def post(self, hwnd: int, message: int, wparam: int, lparam: int) -> None:
        """Post a message to a window without waiting for it."""
        self._gui.PostMessage(hwnd, message, wparam, lparam)


@dataclass
class DialogWatch:
    """Watch process ``pid`` for a dialog left waiting, while a call runs.

    Use it as a context manager around the blocking call. Dialogs already
    open when it starts are ignored, but for the ones ``only_known`` answers
    (the memory warning may come up between two calls).

    Attributes:
        pid: The SolidWorks process ID.
        timeout: Seconds a dialog may stay open before it is answered.
        windows: The window calls; the real Win32 ones by default.
        poll: Seconds between looks.
        clock: The time source, in seconds.
        only_known: Answer only the dialogs in ``KNOWN_ANSWERS``.
        name: What is watched, for the log and the thread's name.
        seen: Every dialog answered or left, in the order found.
    """

    pid: int
    timeout: float
    windows: Windows = field(default_factory=Win32Windows)
    poll: float = 0.5
    clock: Callable[[], float] = time.monotonic
    only_known: bool = False
    name: str = "macro"
    seen: list[DialogSeen] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Set up the thread state."""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._before: set[int] = set()
        self._first: dict[int, float] = {}
        self._handled: set[int] = set()
        self._pressed: dict[int, tuple[int, str, int]] = {}

    def __enter__(self) -> DialogWatch:
        """Note the dialogs already open and start watching; never fail the call."""
        try:
            self._before = set(self.windows.dialogs(self.pid))
            thread = threading.Thread(
                target=self._run, name=f"{self.name}-dialog-watch", daemon=True
            )
            thread.start()
        except Exception as exc:  # noqa: BLE001 - a watch never fails the call
            logger.warning("[{}] the dialog watch could not start: {}", self.name, exc)
            return self
        self._thread = thread
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop watching."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        """Look every ``poll`` seconds until stopped; a failure ends the watch."""
        try:
            while not self._stop.wait(self.poll):
                self.check()
        except Exception as exc:  # noqa: BLE001 - a watch never fails the call
            logger.warning("[{}] the dialog watch stopped: {}", self.name, exc)

    def check(self) -> None:
        """Answer any new dialog that has been open longer than the timeout.

        A dialog still open after its button was pressed is pressed again,
        up to ``PRESSES`` times: a click can miss a window that isn't active.
        """
        now = self.clock()
        for hwnd in self.windows.dialogs(self.pid):
            if hwnd in self._pressed:
                button, label, presses = self._pressed[hwnd]
                # A dialog showing its own prompt is disabled: pressing again
                # would stack another prompt.
                if presses < PRESSES and self.windows.enabled(hwnd):
                    self._pressed[hwnd] = (button, label, presses + 1)
                    self._press(hwnd, button, label)
                continue
            if hwnd in self._handled or (hwnd in self._before and not self.only_known):
                continue
            opened = self._first.setdefault(hwnd, now)
            if now - opened >= self.timeout:
                self._handled.add(hwnd)
                self._answer(hwnd)

    def _answer(self, hwnd: int) -> None:
        """Read one dialog and answer it (see the module's rules)."""
        kids = self.windows.children(hwnd)
        text = " ".join(
            " ".join(_LINK_TAG.sub("", t).split())
            for _h, cls, t in kids
            if cls in TEXT_CLASSES and t.strip()
        )
        buttons = [
            (h, t.replace("&", "").strip()) for h, cls, t in kids if cls == "Button"
        ]
        labels = tuple(label for _h, label in buttons)
        safe = [
            (h, label)
            for fragment, wanted in KNOWN_ANSWERS
            if fragment in text.lower()
            for h, label in buttons
            if label.lower() == wanted.lower()
        ]
        cancel = [(h, label) for h, label in buttons if label.lower() == "cancel"]
        answer: str | None = None
        if safe or (not self.only_known and (cancel or len(buttons) == 1)):
            button, label = (safe or cancel or buttons)[0]
            if self._press(hwnd, button, label):
                answer = label
                self._pressed[hwnd] = (button, label, 1)
        elif not self.only_known and not buttons:
            answer = "closed"
            self.windows.post(hwnd, _WM_CLOSE, 0, 0)
        elif hwnd in self._before:
            return  # open before this call and not one to answer: not its business
        seen = DialogSeen(
            self.windows.title(hwnd), text, labels, answer, hwnd in self._before
        )
        self.seen.append(seen)
        if answer is None:
            logger.warning(
                "[{}] a dialog needs the user: {}", self.name, seen.describe()
            )
        else:
            logger.warning("[{}] a dialog was waiting: {}", self.name, seen.describe())

    def _press(self, hwnd: int, button: int, label: str) -> bool:
        """Click a dialog's button: WM_COMMAND with its ID (BN_CLICKED is 0).

        A task dialog's buttons have no ID (SolidWorks' memory warning, seen
        live): the button gets a click message of its own, and the dialog is
        asked to click its common button of that label.

        Returns:
            bool: Whether a message went through; a window gone in between, or
            one Windows won't let us post to, refuses them.
        """
        sent = False
        try:
            control = self.windows.control_id(button)
            if control:
                self.windows.post(hwnd, _WM_COMMAND, control, button)
                return True
            self.windows.post(button, _BM_CLICK, 0, 0)
            sent = True
            common = _COMMON_IDS.get(label.lower())
            if common:
                self.windows.post(hwnd, _TDM_CLICK_BUTTON, common, 0)
        except Exception as exc:  # noqa: BLE001 - the dialog may have closed
            logger.debug("[{}] a press missed: {}", self.name, exc)
        return sent


def refused_message(operation: str, seen: list[DialogSeen]) -> str:
    """The error for a call SolidWorks stopped with a dialog the watch answered.

    Args:
        operation: The adapter operation.
        seen: The dialogs answered.

    Returns:
        str: The dialog, its text and the answer given.
    """
    dialogs = "; ".join(d.describe() for d in seen)
    return f"SolidWorks stopped {operation} with a dialog: {dialogs}."


def waited_message(seen: list[DialogSeen], timeout: float) -> str:
    """The error for a macro that waited on dialogs.

    Args:
        seen: The dialogs the watch found.
        timeout: The timeout that was used, in seconds.

    Returns:
        str: What happened, and that macros here must not wait for the user.
    """
    dialogs = "; ".join(d.describe() for d in seen)
    return (
        f"The macro waited for the user: after {timeout:g} s a dialog was still "
        f"open, {dialogs}. It may have done part of its work before that. "
        "Macros run by execute_macro must not wait for the user: remove the "
        "MsgBox, form or prompt, or raise dialog_timeout."
    )
