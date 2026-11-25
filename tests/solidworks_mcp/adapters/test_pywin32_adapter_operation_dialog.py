"""Every adapter call runs inside a dialog watch on the SolidWorks process.

SolidWorks' low-memory warning blocked a call until someone answered it: a
headless run waited 30 minutes on it (seen live).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import solidworks_mcp.adapters.pywin32_adapter as module
from solidworks_mcp.adapters.base import AdapterResultStatus
from solidworks_mcp.adapters.solidworks.dialog_watch import DialogSeen
from tests.solidworks_mcp.adapters.solidworks.test_io import _build_adapter

_MEMORY = DialogSeen(
    "SOLIDWORKS",
    "WARNING! Your system is running critically low on memory.",
    ("Yes", "No"),
    "No",
)


class FakeWatch:
    """Stands in for DialogWatch: records how it was made and what it 'saw'."""

    made: list[dict[str, Any]] = []
    seen_next: list[DialogSeen] = []

    def __init__(self, pid: int, timeout: float, **options: Any) -> None:
        FakeWatch.made.append({"pid": pid, "timeout": timeout, **options})
        self.seen = list(FakeWatch.seen_next)

    def __enter__(self) -> FakeWatch:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> Any:
    FakeWatch.made, FakeWatch.seen_next = [], []
    monkeypatch.setattr(module, "DialogWatch", FakeWatch)
    built = _build_adapter(monkeypatch)
    built._watch_pid = 4242
    return built


def test_a_call_runs_inside_a_watch_for_known_dialogs(adapter) -> None:
    result = adapter._handle_com_operation("create_part", lambda: "made")

    assert result.status == AdapterResultStatus.SUCCESS and result.data == "made"
    assert FakeWatch.made == [
        {
            "pid": 4242,
            "timeout": module.OPERATION_DIALOG_SECONDS,
            "only_known": True,
            "name": "operation",
        }
    ]


def test_a_call_stopped_by_the_memory_warning_is_an_error(adapter) -> None:
    FakeWatch.seen_next = [_MEMORY]

    result = adapter._handle_com_operation("create_trim_extend", lambda: "part")

    assert result.status == AdapterResultStatus.ERROR
    assert result.data == "part"  # what it did before the command was stopped
    assert result.error == (
        "SolidWorks stopped create_trim_extend with a dialog: 'SOLIDWORKS': "
        "'WARNING! Your system is running critically low on memory.' "
        "(answered 'No')."
    )


def test_a_memory_warning_up_before_the_call_is_not_its_failure(adapter) -> None:
    FakeWatch.seen_next = [
        DialogSeen(_MEMORY.title, _MEMORY.text, _MEMORY.buttons, "No", True)
    ]

    assert adapter._handle_com_operation("create_extrusion", lambda: 5).data == 5


def test_the_call_s_own_error_is_kept(adapter) -> None:
    FakeWatch.seen_next = [_MEMORY]

    def fails() -> None:
        raise ValueError("the trim found no boundary")

    result = adapter._handle_com_operation("create_trim_extend", fails)

    assert result.error.startswith(
        "Error in create_trim_extend: the trim found no boundary SolidWorks stopped"
    )


def test_a_dialog_left_for_the_user_changes_nothing(adapter) -> None:
    FakeWatch.seen_next = [DialogSeen("Progress", "", ("Cancel",), None)]

    assert adapter._handle_com_operation("save", lambda: 1).data == 1


def test_a_call_inside_a_call_is_watched_once(adapter) -> None:
    def outer() -> Any:
        return adapter._handle_com_operation("inner", lambda: 2).data

    assert adapter._handle_com_operation("outer", outer).data == 2
    assert len(FakeWatch.made) == 1 and adapter._watching is False


def test_a_watch_that_cannot_be_made_never_stops_the_call(adapter, monkeypatch) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise ImportError("No module named 'win32gui'")

    monkeypatch.setattr(module, "DialogWatch", broken)

    assert adapter._handle_com_operation("x", lambda: 4).data == 4


def test_without_a_solidworks_process_nothing_is_watched(adapter) -> None:
    adapter._watch_pid = None

    assert adapter._handle_com_operation("x", lambda: 3).data == 3
    assert FakeWatch.made == []


@pytest.mark.asyncio
async def test_connect_finds_the_process_and_disconnect_forgets_it(adapter) -> None:
    async def connect() -> None:
        adapter.swApp = SimpleNamespace(GetProcessID=lambda: 5150)

    async def disconnect() -> None:
        adapter.swApp = None

    adapter._session_coordinator = SimpleNamespace(
        connect=connect, disconnect=disconnect
    )

    await adapter.connect()
    assert adapter._watch_pid == 5150
    await adapter.disconnect()
    assert adapter._watch_pid is None

    async def connect_unknown() -> None:
        adapter.swApp = SimpleNamespace()  # no process ID to read

    adapter._session_coordinator.connect = connect_unknown
    await adapter.connect()
    assert adapter._watch_pid is None
