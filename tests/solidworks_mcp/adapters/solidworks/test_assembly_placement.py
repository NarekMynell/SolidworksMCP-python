"""Tests for placing components: AddComponents3, centring, moving, fixing."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
from solidworks_mcp.adapters.solidworks import assembly, placement
from solidworks_mcp.adapters.solidworks.assembly import SolidWorksAssemblyMixin


def _values(array: Any) -> list[Any]:
    """The items of a COM array, whether a VARIANT or a plain tuple."""
    return list(getattr(array, "value", array))


class _Transform:
    def __init__(self, data: list[float]) -> None:
        self.ArrayData = tuple(data)


class _Placed:
    """A component AddComponents3 put in: a transform and a box that follows it."""

    def __init__(self, name: str, path: str, data: list[float]) -> None:
        self.Name2 = name
        self.path = path
        self._data = list(data)
        self.size = (1.0, 0.2, 0.1)  # an unturned 1 m beam, from its origin along +x
        self.held = False
        self.selected = False

    @property
    def Transform2(self) -> _Transform:
        return _Transform(self._data)

    @Transform2.setter
    def Transform2(self, transform: _Transform) -> None:
        if not self.held:
            self._data = list(transform.ArrayData)

    def GetBox(self, _planes: bool, _sketches: bool) -> tuple[float, ...]:
        o = self._data[9:12]
        return (*o, *(o[i] + self.size[i] for i in range(3)))

    def Select4(self, _append: bool, _data: Any, _callout: bool) -> bool:
        self.selected = True
        return True


class _Assembly:
    """An assembly that keeps what AddComponents3 put in it."""

    def __init__(self) -> None:
        self.components: dict[str, _Placed] = {}
        self.refuse: set[str] = set()
        self.calls: list[tuple[Any, ...]] = []
        self.SelectionManager = SimpleNamespace(
            CreateSelectData=lambda: SimpleNamespace(Mark=None)
        )

    def GetComponentCount(self, _top_level: bool) -> int:
        return len(self.components)

    def AddComponents3(self, names: Any, transforms: Any, systems: Any) -> Any:
        paths, data = _values(names), _values(transforms)
        self.calls.append(("AddComponents3", paths, data, _values(systems)))
        added: list[Any] = []
        for index, path in enumerate(paths):
            if path in self.refuse:
                added.append(None)
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            count = sum(1 for c in self.components.values() if c.path == path)
            component = _Placed(
                f"{stem}-{count + 1}", path, data[16 * index : 16 * index + 16]
            )
            self.components[component.Name2] = component
            added.append(component)
        return tuple(added)

    def FixComponent(self) -> None:
        picked = sorted(c.Name2 for c in self.components.values() if c.selected)
        self.calls.append(("FixComponent", picked))

    def ClearSelection2(self, _all: bool) -> bool:
        for component in self.components.values():
            component.selected = False
        return True

    def GraphicsRedraw2(self) -> None:
        self.calls.append(("GraphicsRedraw2",))


class _Adapter(SolidWorksAssemblyMixin):
    def __init__(self, doc_type: int = 2) -> None:
        self.currentModel = _Assembly()
        self.doc_type = doc_type
        utility = SimpleNamespace(CreateTransform=lambda a: _Transform(_values(a)))
        self.swApp = SimpleNamespace(GetMathUtility=lambda: utility)

    def _attempt(self, fn: Any, default: Any = None) -> Any:
        try:
            return fn()
        except Exception:
            return default

    def _handle_com_operation(self, _name: str, fn: Any) -> AdapterResult[Any]:
        try:
            return AdapterResult(status=AdapterResultStatus.SUCCESS, data=fn())
        except Exception as exc:
            return AdapterResult(status=AdapterResultStatus.ERROR, error=str(exc))


@pytest.fixture(autouse=True)
def _fake_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the io helpers at the fake assembly."""

    def find(adapter: Any, model: Any, names: list[str]) -> dict[str, Any]:
        missing = [n for n in names if n not in model.components]
        if missing:
            raise Exception(f"Component(s) not found: {', '.join(missing)}.")
        return {n: model.components[n] for n in names}

    monkeypatch.setattr(assembly, "_doc_type", lambda a: a.doc_type)
    monkeypatch.setattr(assembly, "_find_components", find)
    monkeypatch.setattr(assembly, "_wrap_component", lambda _a, raw: raw)


@pytest.fixture
def beam(tmp_path: Path) -> str:
    path = tmp_path / "beam.SLDPRT"
    path.write_bytes(b"")
    return str(path)


@pytest.mark.asyncio
async def test_add_components_places_origins_and_turns_in_one_call(beam: str) -> None:
    adapter = _Adapter()

    result = await adapter.add_components(
        [
            {"file_path": beam, "x": 0, "y": 0, "z": 0},
            {"file_path": beam, "x": 2000, "rz": 90},
        ]
    )

    assert result.is_success, result.error
    calls = [c for c in adapter.currentModel.calls if c[0] == "AddComponents3"]
    assert len(calls) == 1
    _, paths, data, systems = calls[0]
    assert paths == [beam, beam] and systems == ["", ""]
    assert data[16:32] == pytest.approx(
        placement.to_array((2000, 0, 0), placement.rotation(rz=90))
    )
    added = result.data["added"]
    assert [a["component"] for a in added] == ["beam-1", "beam-2"]
    assert added[1]["origin_mm"] == pytest.approx([2000, 0, 0])
    assert added[1]["rotation_deg"] == pytest.approx([0, 0, 90])
    assert (result.data["components_before"], result.data["components_after"]) == (0, 2)
    assert result.data["failed"] == [] and result.data["fixed"] is False


@pytest.mark.asyncio
async def test_a_missing_file_fails_its_own_item_and_the_rest_go_in(
    beam: str, tmp_path: Path
) -> None:
    adapter = _Adapter()
    drawing = tmp_path / "frame.SLDDRW"
    drawing.write_bytes(b"")

    result = await adapter.add_components(
        [
            {"file_path": str(tmp_path / "gone.sldprt")},
            {"file_path": beam},
            {"file_path": str(drawing)},
        ]
    )

    assert result.is_success
    assert [a["index"] for a in result.data["added"]] == [1]
    assert [(f["index"], f["error"]) for f in result.data["failed"]] == [
        (0, "file not found"),
        (2, "not a .sldprt or .sldasm file"),
    ]


@pytest.mark.asyncio
async def test_no_file_at_all_is_an_error_before_solidworks_is_asked(
    tmp_path: Path,
) -> None:
    adapter = _Adapter()

    result = await adapter.add_components(
        [{"file_path": ""}, {"file_path": str(tmp_path / "x.sldprt")}]
    )

    assert result.is_error
    assert "No component could be added: <empty>: file not found" in result.error
    assert adapter.currentModel.calls == []


@pytest.mark.asyncio
async def test_a_part_solidworks_refuses_fails_alone(beam: str, tmp_path: Path) -> None:
    adapter = _Adapter()
    empty = tmp_path / "empty.sldprt"
    empty.write_bytes(b"")
    adapter.currentModel.refuse = {str(empty)}

    result = await adapter.add_components(
        [{"file_path": str(empty)}, {"file_path": beam}]
    )

    assert result.is_success
    assert [f["index"] for f in result.data["failed"]] == [0]
    assert "no solid body" in result.data["failed"][0]["error"]


@pytest.mark.asyncio
async def test_nothing_added_by_solidworks_is_an_error(beam: str) -> None:
    adapter = _Adapter()
    adapter.currentModel.refuse = {beam}

    result = await adapter.add_components([{"file_path": beam}])

    assert result.is_error
    assert "added none of the components" in result.error


@pytest.mark.asyncio
async def test_fix_fixes_every_new_component_in_one_call(beam: str) -> None:
    adapter = _Adapter()

    result = await adapter.add_components(
        [{"file_path": beam}, {"file_path": beam, "x": 500}], fix=True
    )

    assert result.data["fixed"] is True
    fixes = [c for c in adapter.currentModel.calls if c[0] == "FixComponent"]
    assert fixes == [("FixComponent", ["beam-1", "beam-2"])]


@pytest.mark.asyncio
async def test_anchor_center_puts_the_box_middle_on_the_point(beam: str) -> None:
    adapter = _Adapter()

    result = await adapter.add_components(
        [{"file_path": beam, "x": 0, "y": 0, "z": 0, "anchor": "center"}]
    )

    # The 1000 x 200 x 100 mm box starts at the origin, so the origin moves
    # back by half of it.
    assert result.data["added"][0]["origin_mm"] == pytest.approx([-500, -100, -50])


@pytest.mark.asyncio
async def test_anchor_center_needs_the_box(
    beam: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _Adapter()
    monkeypatch.setattr(_Placed, "GetBox", lambda self, a, b: None)

    result = await adapter.add_components([{"file_path": beam, "anchor": "center"}])

    assert result.is_error
    assert "bounding box" in result.error and "anchor 'origin'" in result.error


@pytest.mark.asyncio
async def test_the_component_count_falls_back_to_the_name_walk(
    beam: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _Adapter()
    monkeypatch.setattr(_Assembly, "GetComponentCount", lambda self, top: None)
    monkeypatch.setattr(assembly, "_component_names", lambda a, m: list(m.components))

    result = await adapter.add_components([{"file_path": beam}])

    assert result.data["components_after"] == 1


@pytest.mark.asyncio
async def test_add_components_needs_an_assembly(beam: str) -> None:
    result = await _Adapter(doc_type=1).add_components([{"file_path": beam}])

    assert result.is_error
    assert "add_components requires an assembly document" in result.error


@pytest.mark.asyncio
async def test_insert_component_is_one_centred_item(beam: str) -> None:
    adapter = _Adapter()

    result = await adapter.insert_component(beam, 100, 200, 300, rz=90)

    assert result.is_success, result.error
    assert result.data["component"] == "beam-1"
    assert result.data["position"] == {"x": 100, "y": 200, "z": 300}
    assert result.data["anchor"] == "center"
    assert result.data["rotation_deg"] == pytest.approx([0, 0, 90])
    assert (result.data["components_before"], result.data["components_after"]) == (0, 1)


@pytest.mark.asyncio
async def test_insert_component_passes_an_error_on(tmp_path: Path) -> None:
    result = await _Adapter().insert_component(str(tmp_path / "gone.sldprt"))

    assert result.is_error
    assert "file not found" in result.error


@pytest.mark.asyncio
async def test_an_absolute_move_keeps_what_is_left_out(beam: str) -> None:
    adapter = _Adapter()
    await adapter.add_components(
        [{"file_path": beam, "x": 10, "y": 20, "z": 30, "rx": 90}]
    )

    result = await adapter.set_component_position(["beam-1"], z=1500, rz=45)

    assert result.is_success, result.error
    moved = result.data["moved"][0]
    assert moved["before"]["origin_mm"] == pytest.approx([10, 20, 30])
    assert moved["after"]["origin_mm"] == pytest.approx([10, 20, 1500])
    assert moved["after"]["rotation_deg"] == pytest.approx([90, 0, 45])
    assert result.data["relative"] is False


@pytest.mark.asyncio
async def test_an_absolute_move_without_angles_keeps_the_turn(beam: str) -> None:
    adapter = _Adapter()
    await adapter.add_components([{"file_path": beam, "ry": 30}])

    result = await adapter.set_component_position(["beam-1"], x=5)

    assert result.data["moved"][0]["after"]["rotation_deg"] == pytest.approx([0, 30, 0])


@pytest.mark.asyncio
async def test_a_relative_move_shifts_and_turns_each_component(beam: str) -> None:
    adapter = _Adapter()
    await adapter.add_components([{"file_path": beam}, {"file_path": beam, "x": 1000}])

    result = await adapter.set_component_position(
        ["beam-1", "beam-2"], y=-20, rz=90, relative=True
    )

    after = [m["after"] for m in result.data["moved"]]
    assert [a["origin_mm"] for a in after] == [
        pytest.approx([0, -20, 0]),
        pytest.approx([1000, -20, 0]),
    ]
    assert all(a["rotation_deg"] == pytest.approx([0, 0, 90]) for a in after)


@pytest.mark.asyncio
async def test_a_component_that_does_not_move_is_an_error(beam: str) -> None:
    adapter = _Adapter()
    await adapter.add_components([{"file_path": beam}])
    adapter.currentModel.components["beam-1"].held = True

    result = await adapter.set_component_position(["beam-1"], x=100)

    assert result.is_error
    assert "kept beam-1 where it was" in result.error


@pytest.mark.asyncio
async def test_moving_needs_an_assembly() -> None:
    result = await _Adapter(doc_type=3).set_component_position(["a-1"], x=1)

    assert result.is_error
    assert "set_component_position requires an assembly document" in result.error
