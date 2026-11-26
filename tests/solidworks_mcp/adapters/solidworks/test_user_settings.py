"""The user's SolidWorks options: off only while an operation runs, then back."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from solidworks_mcp.adapters.base import AdapterResultStatus
from solidworks_mcp.adapters.solidworks import user_settings
from solidworks_mcp.adapters.solidworks.user_settings import (
    AUTOMATION_OFF,
    UserSettings,
    notes_folder,
    process_alive,
)


class FakeApp:
    """``ISldWorks`` preference toggles kept in a dict; reads or writes can fail."""

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
    """A user with Input dimension value and the over-define prompt on, the rest off."""
    return {10: True, 100: True, 372: False, 520: False, 642: False}


def test_only_the_options_that_are_on_go_off_and_exactly_those_come_back(
    tmp_path: Path,
) -> None:
    app = FakeApp(_users())
    settings = UserSettings(tmp_path)
    with settings.automation(app):
        assert all(app.toggles[t] is False for t in AUTOMATION_OFF)
        note = json.loads(settings.note.read_text(encoding="utf-8"))
        assert note == {"10": True, "100": True}
    assert app.toggles == _users()
    # The ones already off were never touched.
    assert {t for t, _ in app.sets} == {10, 100}
    assert not settings.note.exists()


def test_a_nested_operation_shares_the_outer_one(tmp_path: Path) -> None:
    app = FakeApp(_users())
    settings = UserSettings(tmp_path)
    with settings.automation(app):
        with settings.automation(app):
            assert app.toggles[10] is False
        # The inner block ending puts nothing back: the outer call still runs.
        assert app.toggles[10] is False
    assert app.toggles == _users()
    assert len(app.sets) == 4  # two off, two back


def test_a_failing_operation_still_puts_them_back(tmp_path: Path) -> None:
    app = FakeApp(_users())
    settings = UserSettings(tmp_path)
    with pytest.raises(ValueError), settings.automation(app):
        raise ValueError("the operation failed")
    assert app.toggles == _users()
    assert not settings.note.exists()


def test_nothing_changes_without_solidworks(tmp_path: Path) -> None:
    settings = UserSettings(tmp_path)
    with settings.automation(None):
        pass
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("answer", [Mock(), 1, "yes", None])
def test_an_answer_that_is_not_a_real_true_is_left_alone(
    tmp_path: Path, answer: Any
) -> None:
    app = Mock()
    app.GetUserPreferenceToggle.return_value = answer
    settings = UserSettings(tmp_path)
    with settings.automation(app):
        pass
    app.SetUserPreferenceToggle.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_an_unreadable_option_is_left_as_it_is(tmp_path: Path) -> None:
    app = FakeApp(_users())
    app.fail_get = {10}
    settings = UserSettings(tmp_path)
    with settings.automation(app):
        assert app.toggles[10] is True
        assert app.toggles[100] is False
    assert app.toggles == _users()


def test_an_option_solidworks_did_not_take_back_is_tried_after_the_next_operation(
    tmp_path: Path,
) -> None:
    app = FakeApp(_users())
    settings = UserSettings(tmp_path)
    with settings.automation(app):
        app.fail_set = {100}
    assert app.toggles[10] is True
    assert app.toggles[100] is False
    assert json.loads(settings.note.read_text(encoding="utf-8")) == {"100": True}
    # While it is owed, this process's note is its own, not an ended one's.
    assert settings.recover(app) == {}
    app.fail_set = set()
    with settings.automation(app):
        # The next operation's note still holds what is owed.
        note = json.loads(settings.note.read_text(encoding="utf-8"))
        assert note == {"10": True, "100": True}
    assert app.toggles == _users()
    assert not settings.note.exists()


def test_a_note_left_by_a_put_back_that_failed_is_taken_by_the_next_server(
    tmp_path: Path,
) -> None:
    app = FakeApp(_users())
    settings = UserSettings(tmp_path)
    with settings.automation(app):
        app.fail_set = {100}
    app.fail_set = set()
    # The next server is another process: it sees an ended process's note.
    note = settings.note.rename(tmp_path / "4242.json")
    later = UserSettings(tmp_path, alive=lambda pid: False)
    assert later.recover(app) == {100: True}
    assert app.toggles == _users()
    assert not note.exists()


def test_a_dead_servers_note_is_put_back_and_removed(tmp_path: Path) -> None:
    app = FakeApp({10: False, 100: False, 642: False})
    (tmp_path / "4242.json").write_text(json.dumps({"10": True, "642": True}))
    settings = UserSettings(tmp_path, alive=lambda pid: False)
    assert settings.recover(app) == {10: True, 642: True}
    assert app.toggles == {10: True, 100: False, 642: True}
    assert not (tmp_path / "4242.json").exists()


def test_a_running_servers_note_is_left_to_it(tmp_path: Path) -> None:
    app = FakeApp({10: False})
    (tmp_path / "4242.json").write_text(json.dumps({"10": True}))
    settings = UserSettings(tmp_path, alive=lambda pid: True)
    assert settings.recover(app) == {}
    assert app.toggles[10] is False
    assert (tmp_path / "4242.json").exists()


def test_a_note_under_this_process_id_is_an_ended_ones(tmp_path: Path) -> None:
    """Recovering before any call: a note with this pid is a reused id's."""
    app = FakeApp({10: False})
    own = tmp_path / f"{os.getpid()}.json"
    own.write_text(json.dumps({"10": True}))
    settings = UserSettings(tmp_path, alive=lambda pid: True)
    assert settings.recover(app) == {10: True}
    assert not own.exists()


def test_a_note_is_kept_while_solidworks_refuses_it(tmp_path: Path) -> None:
    app = FakeApp({10: False})
    app.fail_set = {10}
    (tmp_path / "4242.json").write_text(json.dumps({"10": True}))
    settings = UserSettings(tmp_path, alive=lambda pid: False)
    assert settings.recover(app) == {}
    assert (tmp_path / "4242.json").exists()


@pytest.mark.parametrize(
    "text", ["not json", "[1, 2]", json.dumps({"149": False, "abc": True})]
)
def test_a_broken_or_foreign_note_changes_nothing(tmp_path: Path, text: str) -> None:
    app = FakeApp({149: True})
    (tmp_path / "4242.json").write_text(text)
    settings = UserSettings(tmp_path, alive=lambda pid: False)
    assert settings.recover(app) == {}
    assert app.sets == []
    assert not (tmp_path / "4242.json").exists()


def test_the_notes_folder_is_per_user_unless_overridden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SOLIDWORKS_MCP_USER_SETTINGS_DIR", str(tmp_path))
    assert notes_folder() == tmp_path
    monkeypatch.delenv("SOLIDWORKS_MCP_USER_SETTINGS_DIR")
    assert notes_folder() == Path.home() / ".solidworks_mcp" / "user-settings"


def test_unit_tests_never_use_the_real_notes_folder() -> None:
    assert notes_folder() != Path.home() / ".solidworks_mcp" / "user-settings"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process check")
def test_process_alive_tells_a_running_process_from_an_ended_one() -> None:
    assert process_alive(os.getpid()) is True
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    assert process_alive(ended.pid) is False
    # No such process at all (ids are multiples of 4; this one is not).
    assert process_alive(0x7FFFFFF1) is False


def test_without_a_note_they_still_go_off_and_come_back(tmp_path: Path) -> None:
    blocked = tmp_path / "a-file"
    blocked.write_text("not a folder")
    app = FakeApp(_users())
    settings = UserSettings(blocked)
    with settings.automation(app):
        assert app.toggles[10] is False
    assert app.toggles == _users()


def test_files_that_are_not_notes_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "readme.json").write_text(json.dumps({"10": True}))
    app = FakeApp({10: False})
    assert UserSettings(tmp_path, alive=lambda pid: False).recover(app) == {}
    assert app.sets == []


def test_the_toggles_are_the_sketch_options_never_the_bom_ones() -> None:
    # 149/150 are swBOMControlIdFromAssembly / swBOMControlMissingRows, which
    # the server once set on every connect believing they muted dialogs.
    assert set(AUTOMATION_OFF) == {10, 100, 372, 520, 642}
    assert user_settings.AUTOMATION_OFF is AUTOMATION_OFF


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
            return "done"

        result = adapter._handle_com_operation("outer", operation)
        assert result.status == AdapterResultStatus.SUCCESS
        assert result.data == "done"
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
