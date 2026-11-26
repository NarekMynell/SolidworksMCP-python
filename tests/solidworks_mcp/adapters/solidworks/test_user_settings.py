"""The user's SolidWorks options: off only while some call runs, then back."""

from __future__ import annotations

import contextlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from loguru import logger

from solidworks_mcp.adapters.base import AdapterResultStatus
from solidworks_mcp.adapters.solidworks import user_settings
from solidworks_mcp.adapters.solidworks.user_settings import (
    AUTOMATION_OFF,
    Lock,
    UserSettings,
    keep_off,
    lock_for,
    notes_folder,
    process_alive,
    process_started,
)


def dead(pid: int, started: float | None) -> bool:
    """Every other process has ended."""
    return False


def running(pid: int, started: float | None) -> bool:
    """Every other process still runs."""
    return True


class FakeApp:
    """``ISldWorks`` preference toggles kept in a dict; reads or writes can fail."""

    RevisionNumber = "33.4.0"

    def __init__(self, toggles: dict[int, bool]) -> None:
        self.toggles = dict(toggles)
        self.fail_get: set[int] = set()
        self.fail_set: set[int] = set()
        self.sets: list[tuple[int, bool]] = []

    def GetUserPreferenceToggle(self, toggle: int) -> bool:  # noqa: N802 - COM name
        if toggle in self.fail_get:
            raise RuntimeError("RPC server unavailable")
        return self.toggles.get(toggle, False)

    def SetUserPreferenceToggle(self, toggle: int, value: bool) -> None:  # noqa: N802
        if toggle in self.fail_set:
            raise RuntimeError("RPC server unavailable")
        self.sets.append((toggle, value))
        self.toggles[toggle] = value


def _users() -> dict[int, bool]:
    """A user with Input dimension value, the over-define and sheet prompts on, the rest off."""
    return {10: True, 100: True, 353: True, 372: False, 520: False, 642: False}


def _ledger(folder: Path) -> dict[str, Any]:
    return dict(json.loads((folder / "ledger-33.json").read_text(encoding="utf-8")))


def _write_ledger(
    folder: Path, owed: dict[int, bool], holders: list[dict[str, Any]]
) -> None:
    document = {"owed": {str(t): v for t, v in owed.items()}, "holders": holders}
    (folder / "ledger-33.json").write_text(json.dumps(document), encoding="utf-8")


@contextlib.contextmanager
def _warnings() -> Iterator[list[str]]:
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        yield lines
    finally:
        logger.remove(sink)


# ---------------------------------------------------------------- one adapter


def test_only_the_options_that_are_on_go_off_and_exactly_those_come_back(
    tmp_path: Path,
) -> None:
    app = FakeApp(_users())
    settings = UserSettings(tmp_path)
    with settings.automation(app):
        assert all(app.toggles[t] is False for t in AUTOMATION_OFF)
        ledger = _ledger(tmp_path)
        assert ledger["owed"] == {"10": True, "100": True, "353": True}
        [holder] = ledger["holders"]
        assert (holder["pid"], holder["id"]) == (os.getpid(), settings.id)
        assert holder["started"] == pytest.approx(process_started())
    assert app.toggles == _users()
    assert {t for t, _ in app.sets} == {10, 100, 353}  # the ones off were never touched
    assert list(tmp_path.iterdir()) == []  # an empty ledger is removed


def test_a_nested_operation_shares_the_outer_one(tmp_path: Path) -> None:
    app = FakeApp(_users())
    settings = UserSettings(tmp_path)
    with settings.automation(app):
        with settings.automation(app):
            assert app.toggles[10] is False
        assert app.toggles[10] is False  # the inner end puts nothing back
    assert app.toggles == _users()
    assert len(app.sets) == 6  # three off, three back


def test_a_failing_operation_still_puts_them_back(tmp_path: Path) -> None:
    app = FakeApp(_users())
    with pytest.raises(ValueError), UserSettings(tmp_path).automation(app):
        raise ValueError("the operation failed")
    assert app.toggles == _users()
    assert list(tmp_path.iterdir()) == []


def test_a_call_stopped_before_it_joined_leaves_nothing_behind(tmp_path: Path) -> None:
    class Stop(BaseException):
        """A Ctrl+C while the options are read."""

    app = FakeApp(_users())

    def interrupted(_toggle: int) -> bool:
        raise Stop

    app.GetUserPreferenceToggle = interrupted  # type: ignore[method-assign]
    with pytest.raises(Stop), UserSettings(tmp_path).automation(app):
        pass
    assert app.sets == []
    assert list(tmp_path.glob("*.json")) == []
    taken: list[bool] = []
    other = threading.Thread(
        target=lambda: taken.append(lock_for(tmp_path).held(2).__enter__())
    )
    other.start()
    other.join(10)
    assert taken == [True]  # the lock was given back


def test_nothing_changes_without_solidworks(tmp_path: Path) -> None:
    with UserSettings(tmp_path).automation(None):
        pass
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("answer", [Mock(), 1, "yes", None])
def test_an_answer_that_is_not_a_real_true_is_left_alone(
    tmp_path: Path, answer: Any
) -> None:
    app = Mock()
    app.GetUserPreferenceToggle.return_value = answer
    with UserSettings(tmp_path).automation(app):
        pass
    app.SetUserPreferenceToggle.assert_not_called()


def test_an_unreadable_option_is_left_as_it_is_and_logged_once(tmp_path: Path) -> None:
    app = FakeApp(_users())
    app.fail_get = {10}
    settings = UserSettings(tmp_path)
    with _warnings() as lines:
        for _ in range(2):
            with settings.automation(app):
                assert app.toggles[10] is True
                assert app.toggles[100] is False
    assert app.toggles == _users()
    assert sum("Input dimension value" in line for line in lines) == 1


def test_an_option_solidworks_will_not_switch_off_is_logged(tmp_path: Path) -> None:
    app = FakeApp(_users())
    app.fail_set = {353}
    with _warnings() as lines, UserSettings(tmp_path).automation(app):
        pass
    assert any("Show sheet format dialog" in line for line in lines)


def test_what_solidworks_did_not_take_back_stays_owed_until_it_does(
    tmp_path: Path,
) -> None:
    app = FakeApp(_users())
    settings = UserSettings(tmp_path)
    with settings.automation(app):
        app.fail_set = {100}
    assert (app.toggles[10], app.toggles[100]) == (True, False)
    assert _ledger(tmp_path) == {"owed": {"100": True}, "holders": []}
    app.fail_set = set()
    with settings.automation(app):
        # Owed values are put back first, then switched off for the call.
        assert app.toggles[100] is False
    assert app.toggles == _users()
    assert list(tmp_path.iterdir()) == []


def test_without_a_ledger_on_disk_they_still_go_off_and_come_back(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "a-file"
    blocked.write_text("not a folder")
    app = FakeApp(_users())
    with _warnings() as lines, UserSettings(blocked).automation(app):
        assert app.toggles[10] is False
    assert app.toggles == _users()
    assert any("Can't write the options ledger" in line for line in lines)


# ---------------------------------------------------------------- helpers inside a call


def test_helpers_switch_off_only_what_their_call_read(tmp_path: Path) -> None:
    app = FakeApp(_users())
    app.fail_get = {100}
    adapter = Mock(swApp=app, _user_settings=UserSettings(tmp_path))
    with adapter._user_settings.automation(app):
        app.toggles[10] = True  # as SolidWorks may on entering a sketch
        keep_off(adapter, (10, 100))
        assert app.toggles[10] is False
        assert app.toggles[100] is True  # unread at the start: never switched
    assert app.toggles == _users()


def test_helpers_outside_a_call_or_without_settings_switch_nothing(
    tmp_path: Path,
) -> None:
    app = FakeApp(_users())
    keep_off(Mock(swApp=app, _user_settings=UserSettings(tmp_path)), (10,))
    keep_off(Mock(swApp=app, _user_settings=None), (10,))
    assert app.sets == []


def test_a_helper_that_cannot_switch_logs_it(tmp_path: Path) -> None:
    app = FakeApp(_users())
    settings = UserSettings(tmp_path)
    with _warnings() as lines, settings.automation(app):
        app.fail_set = {10}
        settings.keep_off(app, (10,))
        app.fail_set = set()
    assert any("Input dimension value" in line for line in lines)


# ---------------------------------------------------------------- several servers


def test_the_last_call_out_puts_them_back_never_an_earlier_one(tmp_path: Path) -> None:
    """Two servers' calls overlapping: the first one out must not restore under the other."""
    app = FakeApp(_users())
    first, second = UserSettings(tmp_path), UserSettings(tmp_path)
    a = first.automation(app)
    b = second.automation(app)
    a.__enter__()
    b.__enter__()
    assert len(_ledger(tmp_path)["holders"]) == 2
    a.__exit__(None, None, None)  # the first call ends while the second runs
    assert all(app.toggles[t] is False for t in AUTOMATION_OFF)
    assert [h["id"] for h in _ledger(tmp_path)["holders"]] == [second.id]
    b.__exit__(None, None, None)
    assert app.toggles == _users()
    assert list(tmp_path.iterdir()) == []


def test_an_option_turned_on_during_another_call_is_owed_too(tmp_path: Path) -> None:
    app = FakeApp(_users())
    first, second = UserSettings(tmp_path), UserSettings(tmp_path)
    with first.automation(app):
        app.toggles[642] = True  # the user switched it on meanwhile
        with second.automation(app):
            assert app.toggles[642] is False
    assert app.toggles == {**_users(), 642: True}


def test_holders_whose_process_ended_are_dropped_and_their_options_put_back(
    tmp_path: Path,
) -> None:
    app = FakeApp({**_users(), 10: False, 642: False})
    dead_holder = {"pid": 4242, "started": 1000.0, "id": "gone"}
    _write_ledger(tmp_path, {10: True, 642: True}, [dead_holder])
    seen: dict[int, bool] = {}
    with UserSettings(tmp_path, alive=dead).automation(app):
        seen = dict(app.toggles)
    assert all(not v for v in seen.values())
    assert (app.toggles[10], app.toggles[642]) == (True, True)
    assert list(tmp_path.iterdir()) == []


def test_a_running_holders_options_stay_off_after_this_call(tmp_path: Path) -> None:
    app = FakeApp({**_users(), 10: False})
    asked: list[tuple[int, float | None]] = []

    def alive(pid: int, started: float | None) -> bool:
        asked.append((pid, started))
        return True

    _write_ledger(
        tmp_path, {10: True}, [{"pid": 4242, "started": 1000.0, "id": "busy"}]
    )
    with UserSettings(tmp_path, alive=alive).automation(app):
        pass
    assert app.toggles[10] is False  # the other server's call still runs
    assert _ledger(tmp_path)["owed"] == {"10": True, "100": True, "353": True}
    assert (4242, 1000.0) in asked


def test_a_connect_puts_back_what_ended_holders_left(tmp_path: Path) -> None:
    app = FakeApp({10: False, 642: False})
    _write_ledger(
        tmp_path, {10: True, 642: True}, [{"pid": 4242, "started": None, "id": "x"}]
    )
    assert UserSettings(tmp_path, alive=dead).recover(app) == {10: True, 642: True}
    assert app.toggles == {10: True, 642: True}
    assert list(tmp_path.iterdir()) == []


def test_a_connect_while_a_holder_runs_changes_nothing(tmp_path: Path) -> None:
    app = FakeApp({10: False})
    _write_ledger(tmp_path, {10: True}, [{"pid": 4242, "started": None, "id": "x"}])
    assert UserSettings(tmp_path, alive=running).recover(app) == {}
    assert app.toggles[10] is False


def test_a_holder_under_this_process_id_from_an_ended_process_is_dropped(
    tmp_path: Path,
) -> None:
    """The id came back to this process: the holder's start time tells them apart."""
    app = FakeApp({10: False})
    _write_ledger(
        tmp_path, {10: True}, [{"pid": os.getpid(), "started": 1.0, "id": "old"}]
    )
    assert UserSettings(tmp_path).recover(app) == {10: True}


def test_another_adapter_of_this_process_counts_as_running(tmp_path: Path) -> None:
    app = FakeApp(_users())
    first, second = UserSettings(tmp_path), UserSettings(tmp_path)
    with first.automation(app):
        assert second.recover(app) == {}
        assert all(app.toggles[t] is False for t in AUTOMATION_OFF)
    assert app.toggles == _users()


def test_a_stale_entry_of_this_adapter_is_replaced_on_its_next_call(
    tmp_path: Path,
) -> None:
    """Its last leave couldn't write the ledger; its next call drops the leftover."""
    app = FakeApp({10: False})
    settings = UserSettings(tmp_path, alive=running)
    holder = {"pid": os.getpid(), "started": process_started(), "id": settings.id}
    _write_ledger(tmp_path, {10: True}, [holder])
    with settings.automation(app):
        assert [h["id"] for h in _ledger(tmp_path)["holders"]] == [settings.id]
    assert app.toggles[10] is True


def test_notes_of_the_first_release_are_still_put_back(tmp_path: Path) -> None:
    app = FakeApp({10: False, 642: False})
    old = tmp_path / "4242.json"
    old.write_text(json.dumps({"10": True, "642": True}), encoding="utf-8")
    assert UserSettings(tmp_path, alive=dead).recover(app) == {10: True, 642: True}
    assert not old.exists()


def test_a_first_release_note_of_a_running_server_is_left_to_it(tmp_path: Path) -> None:
    app = FakeApp({10: False})
    old = tmp_path / "4242.json"
    old.write_text(json.dumps({"10": True}), encoding="utf-8")
    assert UserSettings(tmp_path, alive=running).recover(app) == {}
    assert old.exists()


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[1, 2]",
        json.dumps(
            {"owed": {"149": True, "abc": True}, "holders": [{"pid": "abc"}, 7]}
        ),
        json.dumps({"owed": "x", "holders": "y"}),
    ],
)
def test_a_broken_or_foreign_ledger_never_breaks_a_call(
    tmp_path: Path, text: str
) -> None:
    (tmp_path / "ledger-33.json").write_text(text, encoding="utf-8")
    app = FakeApp(_users())
    with UserSettings(tmp_path, alive=dead).automation(app):
        assert app.toggles[10] is False
    assert app.toggles == _users()
    assert 149 not in app.toggles


@pytest.mark.parametrize("text", ["not json", json.dumps({"10": "x", "y": 1})])
def test_a_broken_first_release_note_is_dropped(tmp_path: Path, text: str) -> None:
    (tmp_path / "4242.json").write_text(text, encoding="utf-8")
    app = FakeApp({10: False})
    assert UserSettings(tmp_path, alive=dead).recover(app) == {}
    assert not (tmp_path / "4242.json").exists()


def test_each_release_has_its_own_ledger(tmp_path: Path) -> None:
    app = FakeApp({10: False})
    _write_ledger(tmp_path, {10: True}, [])
    other = FakeApp({10: False})
    other.RevisionNumber = "34.0.0"  # type: ignore[misc]
    assert UserSettings(tmp_path, alive=dead).recover(other) == {}
    assert UserSettings(tmp_path, alive=dead).recover(app) == {10: True}


# ---------------------------------------------------------------- the lock


def test_the_bookkeeping_of_two_servers_never_interleaves(tmp_path: Path) -> None:
    """While one thread holds the lock, another waits; the lock is held only briefly."""
    lock, other_lock = lock_for(tmp_path), lock_for(tmp_path)
    order: list[str] = []

    def other() -> None:
        with other_lock.held(10) as taken:
            order.append(f"other {taken}")

    with lock.held(10) as taken:
        order.append(f"first {taken}")
        waiting = threading.Thread(target=other)
        waiting.start()
        time.sleep(0.3)
        order.append("first out")
    waiting.join(10)
    assert order == ["first True", "first out", "other True"]


def test_a_killed_servers_lock_and_call_are_taken_over(tmp_path: Path) -> None:
    """A real process takes the lock, joins the holders and is killed mid-call."""
    script = (
        "import json, os, sys, time, win32event\n"
        "folder, name = sys.argv[1], sys.argv[2]\n"
        "handle = win32event.CreateMutex(None, False, name)\n"
        "win32event.WaitForSingleObject(handle, 10000)\n"
        "holder = {'pid': os.getpid(), 'started': None, 'id': 'killed'}\n"
        "ledger = {'owed': {'10': True}, 'holders': [holder]}\n"
        "json.dump(ledger, open(os.path.join(folder, 'ledger-33.json'), 'w'))\n"
        "print('holding', flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), lock_for(tmp_path).name],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "holding"
        child.kill()
        child.wait(10)
        time.sleep(0.5)  # the interpreter behind the venv launcher ends with it
        app = FakeApp({10: False})
        started = time.perf_counter()
        with UserSettings(tmp_path).automation(app):
            pass
        assert time.perf_counter() - started < 5  # the abandoned lock was taken at once
        assert app.toggles[10] is True
        assert list(tmp_path.glob("*.json")) == []
    finally:
        if child.poll() is None:
            child.kill()


def test_a_lock_that_cannot_be_made_or_taken_never_stops_a_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import win32event

    def broken(*_: Any) -> Any:
        raise OSError("no mutex")

    app = FakeApp(_users())
    with monkeypatch.context() as patch:
        patch.setattr(win32event, "CreateMutex", broken)
        with _warnings() as lines, UserSettings(tmp_path).automation(app):
            assert app.toggles[10] is False
        assert any("options lock failed" in line for line in lines)
    assert app.toggles == _users()
    with monkeypatch.context() as patch:
        patch.setattr(
            win32event, "WaitForSingleObject", lambda *_: win32event.WAIT_TIMEOUT
        )
        with _warnings() as lines, UserSettings(tmp_path).automation(app):
            assert app.toggles[10] is False
        assert any("stayed taken" in line for line in lines)
    assert app.toggles == _users()


def test_a_connect_that_cannot_get_the_lock_leaves_the_repair_to_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import win32event

    app = FakeApp({10: False})
    _write_ledger(tmp_path, {10: True}, [])
    monkeypatch.setattr(
        win32event, "WaitForSingleObject", lambda *_: win32event.WAIT_TIMEOUT
    )
    assert UserSettings(tmp_path, alive=dead).recover(app) == {}
    assert app.toggles[10] is False


def test_recover_inside_a_call_does_nothing(tmp_path: Path) -> None:
    app = FakeApp(_users())
    settings = UserSettings(tmp_path)
    with settings.automation(app):
        assert settings.recover(app) == {}
    assert app.toggles == _users()


def test_locks_are_per_notes_folder(tmp_path: Path) -> None:
    assert lock_for(tmp_path).name == lock_for(Path(str(tmp_path).upper())).name
    assert lock_for(tmp_path).name != lock_for(tmp_path / "other").name
    assert isinstance(lock_for(tmp_path), Lock)
    assert lock_for(tmp_path).name.startswith("Local\\solidworks-mcp-")


# ---------------------------------------------------------------- processes and folders


def test_process_alive_tells_a_running_process_from_an_ended_or_reused_one() -> None:
    mine = process_started()
    assert process_alive(os.getpid(), mine) is True
    assert process_alive(os.getpid(), None) is True
    assert process_alive(os.getpid(), (mine or 0.0) - 3600) is False  # a reused id
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    assert process_alive(ended.pid, None) is False
    # No such process at all (ids are multiples of 4; this one is not).
    assert process_alive(0x7FFFFFF1, None) is False
    assert process_started(0x7FFFFFF1) is None


def test_a_process_whose_start_cannot_be_read_counts_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pywintypes
    import win32process

    def refused(_handle: Any) -> Any:
        raise pywintypes.error(5, "GetProcessTimes", "Access is denied.")

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setattr(win32process, "GetProcessTimes", refused)
        assert math.isnan(process_started(child.pid) or 0.0)
        assert process_alive(child.pid, 1.0) is True  # can't tell: never applied
    finally:
        child.kill()
        child.wait(10)


def test_the_notes_folder_is_per_user_unless_overridden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SOLIDWORKS_MCP_USER_SETTINGS_DIR", str(tmp_path))
    assert notes_folder() == tmp_path
    monkeypatch.delenv("SOLIDWORKS_MCP_USER_SETTINGS_DIR")
    assert notes_folder() == Path.home() / ".solidworks_mcp" / "user-settings"


def test_unit_tests_never_use_the_real_notes_folder() -> None:
    assert notes_folder() != Path.home() / ".solidworks_mcp" / "user-settings"


def test_each_test_process_has_its_own_notes_folder() -> None:
    # xdist workers inherit the controller's environment; each sets its own.
    assert str(os.getpid()) in notes_folder().name


def test_the_toggles_are_the_dialog_options_never_the_bom_ones() -> None:
    # 149/150 are swBOMControlIdFromAssembly / swBOMControlMissingRows, which
    # the server once set on every connect believing they muted dialogs.
    assert set(AUTOMATION_OFF) == {10, 100, 353, 372, 520, 642}
    assert user_settings.AUTOMATION_OFF is AUTOMATION_OFF


@pytest.mark.parametrize(
    ("revision", "expected"), [("33.4.0", "33"), (lambda: "34.0.1", "34"), ("", None)]
)
def test_the_release_is_solidworks_major_version(revision: Any, expected: Any) -> None:
    app = Mock()
    app.RevisionNumber = revision
    assert user_settings._version(app) == expected
    del app.RevisionNumber
    assert user_settings._version(app) is None


def test_no_reference_to_solidworks_outlives_a_call(tmp_path: Path) -> None:
    """A COM pointer released after CoUninitialize breaks the next connections (pitfall 48)."""
    import gc
    import weakref

    app = FakeApp(_users())
    settings = UserSettings(tmp_path)
    with settings.automation(app):
        settings.keep_off(app, (10,))
    gone = weakref.ref(app)
    del app
    gc.collect()
    assert gone() is None
    settings.forget()
    assert settings._release is None


def test_the_release_is_read_once_per_connection(tmp_path: Path) -> None:
    reads: list[int] = []

    class Counted(FakeApp):
        @property
        def RevisionNumber(self) -> str:  # type: ignore[override]  # noqa: N802
            reads.append(1)
            return "33.4.0"

    app = Counted(_users())
    settings = UserSettings(tmp_path)
    for _ in range(3):
        with settings.automation(app):
            pass
    assert len(reads) == 1


class TestAdapterOperations:
    """Every adapter operation runs with the options off, and puts them back."""

    @staticmethod
    def _adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app: Any) -> Any:
        from solidworks_mcp.adapters.pywin32_adapter import PyWin32Adapter

        monkeypatch.setattr(
            "solidworks_mcp.adapters.pywin32_adapter.PYWIN32_AVAILABLE", True
        )
        monkeypatch.setattr(
            "solidworks_mcp.adapters.pywin32_adapter.platform.system",
            lambda: "Windows",
        )
        monkeypatch.setenv("SOLIDWORKS_MCP_USER_SETTINGS_DIR", str(tmp_path))
        adapter = PyWin32Adapter({})
        adapter.swApp = app
        return adapter

    def test_an_operation_runs_with_them_off_and_ends_with_the_users_values(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        app = FakeApp(_users())
        adapter = self._adapter(monkeypatch, tmp_path, app)
        seen: list[dict[int, bool]] = []

        def operation() -> str:
            seen.append(dict(app.toggles))
            inner = adapter._handle_com_operation(
                "inner", lambda: seen.append(dict(app.toggles))
            )
            assert inner.status == AdapterResultStatus.SUCCESS
            seen.append(dict(app.toggles))
            return "done"

        result = adapter._handle_com_operation("outer", operation)
        assert result.status == AdapterResultStatus.SUCCESS
        assert result.data == "done"
        assert len(seen) == 3
        assert all(v is False for during in seen for v in during.values())
        assert app.toggles == _users()
        assert list(tmp_path.iterdir()) == []

    def test_a_failed_operation_still_ends_with_the_users_values(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        app = FakeApp(_users())
        adapter = self._adapter(monkeypatch, tmp_path, app)

        def operation() -> None:
            raise RuntimeError("boom")

        result = adapter._handle_com_operation("fails", operation)
        assert result.status == AdapterResultStatus.ERROR
        assert app.toggles == _users()

    def test_the_settle_pause_comes_before_the_options_go_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        app = FakeApp(_users())
        adapter = self._adapter(monkeypatch, tmp_path, app)
        order: list[str] = []
        real_set = app.SetUserPreferenceToggle

        def recording(toggle: int, value: bool) -> None:
            order.append(f"set {toggle} {value}")
            real_set(toggle, value)

        app.SetUserPreferenceToggle = recording  # type: ignore[method-assign]
        monkeypatch.setattr(
            "solidworks_mcp.adapters.pywin32_adapter._settle_after",
            lambda name: order.append(f"settle {name}"),
        )
        adapter._handle_com_operation("probe", lambda: order.append("call"))
        assert order == [
            "set 10 False",
            "set 100 False",
            "set 353 False",
            "call",
            "settle probe",
            "set 10 True",
            "set 100 True",
            "set 353 True",
        ]
