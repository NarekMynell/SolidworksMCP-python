"""The dialog watch that keeps a waiting macro from hanging the server (#97).

Windows are faked; the real Win32 calls are checked against a fake
``win32gui`` and ``win32process``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from solidworks_mcp.adapters.solidworks.dialog_watch import (
    DialogSeen,
    DialogWatch,
    Win32Windows,
    refused_message,
    waited_message,
)

WM_CLOSE, WM_COMMAND, BM_CLICK, TDM_CLICK_BUTTON = 0x0010, 0x0111, 0x00F5, 0x0466


class FakeWindows:
    """Dialogs by hwnd: ``{hwnd: (title, [(child hwnd, class, text)])}``."""

    def __init__(self, dialogs: dict[int, tuple[str, list[tuple[int, str, str]]]]):
        self.open = dict(dialogs)
        self.posted: list[tuple[int, int, int, int]] = []
        self.disabled: set[int] = set()

    def dialogs(self, pid: int) -> list[int]:
        assert pid == 42
        return list(self.open)

    def title(self, hwnd: int) -> str:
        return self.open[hwnd][0]

    def children(self, hwnd: int) -> list[tuple[int, str, str]]:
        return self.open[hwnd][1]

    def control_id(self, hwnd: int) -> int:
        return hwnd % 100

    def enabled(self, hwnd: int) -> bool:
        return hwnd not in self.disabled

    def post(self, hwnd: int, message: int, wparam: int, lparam: int) -> None:
        self.posted.append((hwnd, message, wparam, lparam))


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _watch(windows: FakeWindows, clock: Clock) -> DialogWatch:
    return DialogWatch(42, timeout=5, windows=windows, clock=clock)


_MSGBOX = ("SOLIDWORKS", [(201, "Static", "Done"), (102, "Button", "OK")])


def test_a_dialog_is_answered_only_after_the_timeout() -> None:
    windows, clock = FakeWindows({1: _MSGBOX}), Clock()
    watch = _watch(windows, clock)

    watch.check()
    clock.now = 4.9
    watch.check()
    assert windows.posted == []

    clock.now = 5.0
    watch.check()
    del windows.open[1]  # the press closed it
    watch.check()

    assert windows.posted == [(1, WM_COMMAND, 2, 102)]
    assert watch.seen == [DialogSeen("SOLIDWORKS", "Done", ("OK",), "OK")]


def test_a_dialog_a_press_missed_is_pressed_again_a_few_times() -> None:
    windows, clock = FakeWindows({1: _MSGBOX}), Clock()
    watch = _watch(windows, clock)

    watch.check()
    clock.now = 5.0
    for _ in range(10):
        watch.check()

    assert windows.posted == [(1, WM_COMMAND, 2, 102)] * 5
    assert len(watch.seen) == 1


def test_a_dialog_showing_its_own_prompt_is_not_pressed_again() -> None:
    windows, clock = FakeWindows({1: _MSGBOX}), Clock()
    watch = _watch(windows, clock)

    watch.check()
    clock.now = 5.0
    watch.check()
    windows.disabled.add(1)  # its Cancel opened a confirmation over it
    watch.check()

    assert windows.posted == [(1, WM_COMMAND, 2, 102)]


def test_cancel_wins_over_ok() -> None:
    prompt = (
        "Confirm",
        [
            (301, "Static", "Overwrite?"),
            (101, "Button", "OK"),
            (102, "Button", "&Cancel"),
        ],
    )
    windows, clock = FakeWindows({1: prompt}), Clock()
    watch = _watch(windows, clock)

    watch.check()
    clock.now = 6
    watch.check()

    assert windows.posted == [(1, WM_COMMAND, 2, 102)]
    assert watch.seen[0].answer == "Cancel"
    assert watch.seen[0].buttons == ("OK", "Cancel")


def test_yes_and_no_are_left_for_the_user() -> None:
    question = ("Save?", [(101, "Button", "&Yes"), (102, "Button", "&No")])
    windows, clock = FakeWindows({1: question}), Clock()
    watch = _watch(windows, clock)

    watch.check()
    clock.now = 6
    watch.check()

    assert windows.posted == []
    assert watch.seen == [DialogSeen("Save?", "", ("Yes", "No"), None)]


def test_a_form_without_buttons_is_closed() -> None:
    windows, clock = FakeWindows({1: ("UserForm1", [])}), Clock()
    watch = _watch(windows, clock)

    watch.check()
    clock.now = 6
    watch.check()

    assert windows.posted == [(1, WM_CLOSE, 0, 0)]
    assert watch.seen[0].answer == "closed"


def test_a_dialog_open_before_the_macro_is_ignored() -> None:
    windows, clock = FakeWindows({1: _MSGBOX}), Clock()

    with _watch(windows, clock) as watch:
        clock.now = 60
        watch.check()

    assert windows.posted == []
    assert watch.seen == []


def test_the_thread_answers_a_dialog_while_the_call_runs() -> None:
    windows = FakeWindows({})
    watch = DialogWatch(42, timeout=0, windows=windows, poll=0.01)

    with watch:
        windows.open[7] = _MSGBOX
        deadline = time.monotonic() + 5
        while not watch.seen and time.monotonic() < deadline:
            time.sleep(0.01)

    assert watch.seen[0].answer == "OK"


def test_a_failing_watch_stops_quietly() -> None:
    class Broken(FakeWindows):
        def dialogs(self, pid: int) -> list[int]:
            if self.posted is not None and getattr(self, "started", False):
                raise OSError("EnumWindows failed")
            self.started = True
            return []

    watch = DialogWatch(42, timeout=0, windows=Broken({}), poll=0.01)

    with watch:
        time.sleep(0.05)

    assert watch.seen == []


@pytest.mark.parametrize(
    ("seen", "expected"),
    [
        (
            DialogSeen("SOLIDWORKS", "Done", ("OK",), "OK"),
            "'SOLIDWORKS': 'Done' (answered 'OK')",
        ),
        (DialogSeen("UserForm1", "", (), "closed"), "'UserForm1' (closed)"),
        (
            DialogSeen("Save?", "", ("Yes", "No"), None),
            "'Save?' (left open for the user)",
        ),
    ],
)
def test_describe(seen: DialogSeen, expected: str) -> None:
    assert seen.describe() == expected


# SolidWorks' memory warning, as read live: a task dialog, its message in a
# link control, buttons without IDs (hwnd % 100 == 0 here).
_LOW_MEMORY = (
    "SOLIDWORKS",
    [
        (101, "DirectUIHWND", ""),
        (
            102,
            "SysLink",
            "WARNING! Your system is running critically low on memory.\n"
            "Executing this command might cause SOLIDWORKS to fail.\n\n"
            'Do you want to continue?\n\n<a href="https://x">Online Solutions</a>',
        ),
        (300, "Button", "&Yes"),
        (400, "Button", "&No"),
    ],
)


@pytest.mark.parametrize("only_known", [True, False])
def test_a_known_dialog_gets_its_safe_answer(only_known: bool) -> None:
    windows, clock = FakeWindows({1: _LOW_MEMORY}), Clock()
    watch = DialogWatch(42, 2, windows=windows, clock=clock, only_known=only_known)

    watch.check()
    clock.now = 2
    watch.check()

    # the No button's own click, and the dialog asked to click its IDNO
    assert windows.posted == [(400, BM_CLICK, 0, 0), (1, TDM_CLICK_BUTTON, 7, 0)]
    assert watch.seen[0].answer == "No"
    assert watch.seen[0].text == (
        "WARNING! Your system is running critically low on memory. Executing "
        "this command might cause SOLIDWORKS to fail. Do you want to continue? "
        "Online Solutions"
    )


def test_a_known_dialog_open_before_the_call_is_answered_too() -> None:
    windows, clock = FakeWindows({1: _LOW_MEMORY, 2: _MSGBOX}), Clock()
    watch = DialogWatch(42, 2, windows=windows, clock=clock, only_known=True)
    watch._before = {1, 2}  # both up when the call started

    watch.check()
    clock.now = 2
    watch.check()

    assert [seen.answer for seen in watch.seen] == ["No"]  # the message box isn't ours
    assert watch.seen[0].opened_before is True


def test_a_press_that_does_not_go_through_answers_nothing() -> None:
    class Gone(FakeWindows):
        def post(self, hwnd: int, message: int, wparam: int, lparam: int) -> None:
            raise OSError("invalid window handle")

    windows, clock = Gone({1: _LOW_MEMORY}), Clock()
    watch = DialogWatch(42, 0, windows=windows, clock=clock, only_known=True)

    watch.check()

    assert watch.seen[0].answer is None  # closed by the user, or out of reach


def test_watching_only_known_dialogs_leaves_the_others() -> None:
    windows, clock = FakeWindows({1: _MSGBOX, 2: ("UserForm1", [])}), Clock()
    watch = DialogWatch(42, 2, windows=windows, clock=clock, only_known=True)

    watch.check()
    clock.now = 3
    watch.check()

    assert windows.posted == []
    assert [seen.answer for seen in watch.seen] == [None, None]


def test_a_watch_that_cannot_list_windows_stays_out_of_the_way() -> None:
    class Blind(FakeWindows):
        def dialogs(self, pid: int) -> list[int]:
            raise OSError("no desktop")

    with DialogWatch(42, 2, windows=Blind({})) as watch:
        pass

    assert watch._thread is None and watch.seen == []


def test_a_watch_whose_thread_cannot_start_stays_out_of_the_way(monkeypatch) -> None:
    def no_thread(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(
        "solidworks_mcp.adapters.solidworks.dialog_watch.threading.Thread", no_thread
    )

    with DialogWatch(42, 2, windows=FakeWindows({})) as watch:
        pass

    assert watch._thread is None


def test_the_refusal_names_the_call_and_the_dialog() -> None:
    seen = DialogSeen("SOLIDWORKS", "Memory is low.", ("Yes", "No"), "No")

    assert refused_message("create_part", [seen]) == (
        "SolidWorks stopped create_part with a dialog: 'SOLIDWORKS': "
        "'Memory is low.' (answered 'No')."
    )


def test_the_error_names_the_dialog_and_the_rule() -> None:
    message = waited_message([DialogSeen("SOLIDWORKS", "Done", ("OK",), "OK")], 30)

    assert message.startswith(
        "The macro waited for the user: after 30 s a dialog was still open, "
        "'SOLIDWORKS': 'Done' (answered 'OK')."
    )
    assert "must not wait for the user" in message


class FakeGui:
    """The ``win32gui`` calls ``Win32Windows`` makes."""

    def __init__(self) -> None:
        self.windows: dict[int, dict[str, Any]] = {
            1: {"class": "#32770", "visible": True, "text": "SOLIDWORKS", "pid": 42},
            2: {"class": "#32770", "visible": False, "text": "hidden", "pid": 42},
            3: {"class": "Notepad", "visible": True, "text": "notes", "pid": 42},
            4: {"class": "ThunderDFrame", "visible": True, "text": "Form", "pid": 7},
            11: {"class": "Static", "text": "Done", "parent": 1},
            12: {"class": "Button", "text": "OK", "parent": 1, "id": 2},
        }
        self.posted: list[tuple[int, int, int, int]] = []

    def EnumWindows(self, visit: Any, extra: Any) -> None:
        for hwnd, w in self.windows.items():
            if "parent" not in w:
                visit(hwnd, extra)

    def EnumChildWindows(self, parent: int, visit: Any, extra: Any) -> None:
        for hwnd, w in self.windows.items():
            if w.get("parent") == parent:
                visit(hwnd, extra)

    def IsWindowVisible(self, hwnd: int) -> bool:
        return self.windows[hwnd]["visible"]

    def GetClassName(self, hwnd: int) -> str:
        return self.windows[hwnd]["class"]

    def GetWindowText(self, hwnd: int) -> str:
        return self.windows[hwnd]["text"]

    def GetDlgCtrlID(self, hwnd: int) -> int:
        return self.windows[hwnd]["id"]

    def IsWindowEnabled(self, hwnd: int) -> bool:
        return hwnd == 1

    def PostMessage(self, hwnd: int, message: int, wparam: int, lparam: int) -> None:
        self.posted.append((hwnd, message, wparam, lparam))


def test_win32_windows_reads_and_posts_through_win32gui() -> None:
    gui = FakeGui()
    process = SimpleNamespace(
        GetWindowThreadProcessId=lambda hwnd: (0, gui.windows[hwnd]["pid"])
    )
    windows = Win32Windows(gui, process)

    assert windows.dialogs(42) == [1]
    assert windows.title(1) == "SOLIDWORKS"
    assert windows.children(1) == [(11, "Static", "Done"), (12, "Button", "OK")]
    assert windows.control_id(12) == 2
    assert windows.enabled(1) and not windows.enabled(2)
    windows.post(1, WM_COMMAND, 2, 12)
    assert gui.posted == [(1, WM_COMMAND, 2, 12)]


def test_win32_windows_imports_pywin32_by_default(monkeypatch) -> None:
    import sys

    gui, process = SimpleNamespace(name="gui"), SimpleNamespace(name="process")
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    monkeypatch.setitem(sys.modules, "win32process", process)

    windows = Win32Windows()

    assert (windows._gui, windows._process) == (gui, process)
