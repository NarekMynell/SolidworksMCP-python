"""Tests for galvanizing vent and drain holes: the geometry, and the cut flow against fakes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
from solidworks_mcp.adapters.solidworks import weldment_vents as vents
from solidworks_mcp.adapters.solidworks.weldment_vents import (
    Face,
    SolidWorksWeldmentVentsMixin,
    axis_of,
    vent_holes,
    walls_of,
)


def _tube(x0: float, x1: float, h: float = 100.0, t: float = 5.0) -> list[Face]:
    """An RHS along x from x0 to x1, centred on y = z = 0: four outer and four inner walls."""
    faces = []
    for normal, offset in (
        ((0, 1, 0), h / 2),
        ((0, -1, 0), h / 2),
        ((0, 0, 1), h / 2),
        ((0, 0, -1), h / 2),
    ):
        point = tuple(c * offset for c in normal)
        ends = ((x0, *point[1:]), (x1, *point[1:]))
        faces.append(Face(normal, point, (x1 - x0) * h, ends))  # type: ignore[arg-type]
        inner = tuple(-c for c in normal)
        inner_point = tuple(c * (offset - t) for c in normal)
        faces.append(
            Face(inner, inner_point, (x1 - x0) * (h - 2 * t), ((x0, 0, 0), (x1, 0, 0)))
        )  # type: ignore[arg-type]
    faces.append(Face((1, 0, 0), (x1, 0, 0), h * h - (h - 2 * t) ** 2, ((x1, 0, 0),)))
    return faces


def test_the_axis_comes_from_two_walls() -> None:
    axis = axis_of(_tube(0, 2000))
    assert axis is not None
    assert abs(abs(axis[0]) - 1.0) < 1e-9
    assert axis_of([]) is None
    flat = [Face((0, 1, 0), (0, 0, 0), 10.0, ()), Face((0, -1, 0), (0, 1, 0), 5.0, ())]
    assert axis_of(flat) is None


def test_outer_walls_only_with_their_extent() -> None:
    walls = walls_of(_tube(0, 2000), (1.0, 0.0, 0.0), (1000.0, 0.0, 0.0))
    assert len(walls) == 4
    assert {round(w.distance) for w in walls} == {50}
    assert all(w.start == -1000.0 and w.end == 1000.0 for w in walls)


def test_a_level_member_vents_up_at_one_end_and_drains_down_at_the_other() -> None:
    walls = walls_of(_tube(0, 2000), (1.0, 0.0, 0.0), (1000.0, 0.0, 0.0))
    vent, drain = vent_holes(
        (1.0, 0.0, 0.0), (1000.0, 0.0, 0.0), walls, (0.0, 1.0, 0.0), 30.0
    )
    assert vent.kind == "vent" and vent.normal == (0, 1, 0)
    assert vent.point == (1970.0, 50.0, 0.0)
    assert drain.kind == "drain" and drain.normal == (0, -1, 0)
    assert drain.point == (30.0, -50.0, 0.0)
    assert vent.depth == 50.0


def test_a_sloping_member_vents_at_its_high_end() -> None:
    axis = (-1.0, 0.0, 0.0)  # the member drawn from x 2000 to x 0
    walls = walls_of(_tube(0, 2000), axis, (1000.0, 0.0, 0.0))
    vent, drain = vent_holes(axis, (1000.0, 0.0, 0.0), walls, (-0.3, 0.95, 0.0), 30.0)
    # Up leans toward -x: the -x end hangs higher.
    assert vent.point[0] == 30.0 and vent.point[1] == 50.0
    assert drain.point[0] == 1970.0 and drain.point[1] == -50.0


def test_a_plumb_member_uses_two_opposite_walls() -> None:
    walls = walls_of(_tube(0, 3000), (1.0, 0.0, 0.0), (1500.0, 0.0, 0.0))
    vent, drain = vent_holes(
        (1.0, 0.0, 0.0), (1500.0, 0.0, 0.0), walls, (1.0, 0.0, 0.0), 40.0
    )
    assert vent.point[0] == 2960.0 and drain.point[0] == 40.0
    assert tuple(-c for c in vent.normal) == drain.normal


def test_too_few_walls_make_no_holes() -> None:
    assert vent_holes((1.0, 0.0, 0.0), (0.0, 0.0, 0.0), [], (0.0, 1.0, 0.0), 30.0) == []


# --- the flow, against fakes ----------------------------------------------------


class _Body:
    def __init__(
        self, name: str, faces: list[Face] | None, centre=(1000.0, 0.0, 0.0)
    ) -> None:
        self.Name, self.faces, self.centre = name, faces, centre

    def GetMassProperties(self, _density: float) -> list[float]:
        return [c / 1000.0 for c in self.centre] + [0.0]


class _Model:
    def __init__(self) -> None:
        self.selected: list[Any] = []
        self.cuts: list[tuple[Any, ...]] = []
        self.features: list[Any] = []
        manager = SimpleNamespace(ActiveSketch=None, AddToDB=False, AutoSolve=True)
        manager.InsertSketch = lambda _r: self._toggle()
        manager.CreateCircleByRadius = lambda *a: object()
        self.SketchManager = manager
        self.FeatureManager = SimpleNamespace(FeatureCut4=self._cut)
        self.Extension = SimpleNamespace(SelectByID2=lambda *a: True)

    def _toggle(self) -> None:
        manager = self.SketchManager
        if manager.ActiveSketch is None:
            manager.ActiveSketch = SimpleNamespace()
        else:
            self.features.append(
                SimpleNamespace(Name="Sketch", Select2=lambda *a: True)
            )
            manager.ActiveSketch = None

    def _cut(self, *args: Any) -> Any:
        self.cuts.append(args)
        feature = SimpleNamespace(Name=f"Cut-Extrude{len(self.cuts)}")
        self.features.append(feature)
        return feature

    def FeatureByPositionReverse(self, _i: int) -> Any:
        return self.features[-1]

    def ClearSelection2(self, _all: bool) -> None:
        self.selected = []

    def GraphicsRedraw2(self) -> None:
        pass


class _Adapter(SolidWorksWeldmentVentsMixin):
    def __init__(self, model: Any) -> None:
        self.currentModel = model

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


@pytest.fixture
def bodies(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    named = {
        "Rails[1]": _Body("Rails[1]", _tube(0, 2000)),
        "Pipe[1]": _Body("Pipe[1]", None),
        "Slim[1]": _Body("Slim[1]", _tube(0, 2000, h=12.0, t=1.0)),
    }
    seen: dict[str, Any] = {"folders": [], "tied": 0}
    monkeypatch.setattr(vents, "_need_part", lambda _a, _op: None)
    monkeypatch.setattr(
        vents, "_named_bodies", lambda _a, _m, _p, names: {n: named[n] for n in names}
    )
    monkeypatch.setattr(vents, "_faces", lambda _a, body: body.faces or [])
    monkeypatch.setattr(
        vents,
        "_select_wall",
        lambda _a, model, body, _hole: model.Extension.SelectByID2() is not False,
    )
    monkeypatch.setattr(vents, "_body_of", lambda _a, feature: f"{feature.Name} body")
    monkeypatch.setattr(vents, "_to_sketch", lambda _a, _s, p: list(p))
    monkeypatch.setattr(
        vents,
        "tie_points_to_origin",
        lambda *a: seen.__setitem__("tied", seen["tied"] + 1),
    )

    def into(_a: Any, _m: Any, folder: str, names: list[str]) -> dict[str, Any]:
        seen["folders"].append((folder, names))
        return {"folder": folder, "moved": names, "missing": [], "refused": []}

    monkeypatch.setattr(vents, "move_into_folder", into)
    return seen


@pytest.mark.asyncio
async def test_each_hollow_member_gets_a_vent_and_a_drain(
    bodies: dict[str, Any],
) -> None:
    model = _Model()
    result = await _Adapter(model).create_vent_holes(
        {
            "members": ["Rails[1]", "Pipe[1]", "Slim[1]"],
            "diameter": 12,
            "folder": "Vent holes",
        }
    )
    assert result.status == AdapterResultStatus.SUCCESS, result.error
    holes = result.data["holes"]
    assert [(h["member"], h["kind"]) for h in holes] == [
        ("Rails[1]", "vent"),
        ("Rails[1]", "drain"),
    ]
    assert holes[0]["feature"] == "Rails[1] vent Ø12 (1)"
    assert [s["member"] for s in result.data["skipped"]] == ["Pipe[1]", "Slim[1]"]
    # Blind to the axis: 50 mm into a 100 mm tube.
    assert model.cuts[0][5] == pytest.approx(0.05)
    assert bodies["tied"] == 2
    assert bodies["folders"][0][0] == "Vent holes"


@pytest.mark.asyncio
async def test_refusals_come_back_as_errors(bodies: dict[str, Any]) -> None:
    busy = _Model()
    busy.SketchManager.ActiveSketch = object()
    result = await _Adapter(busy).create_vent_holes(
        {"members": ["Rails[1]"], "diameter": 12}
    )
    assert "A sketch is open" in str(result.error)

    no_face = _Model()
    no_face.Extension.SelectByID2 = lambda *a: False
    result = await _Adapter(no_face).create_vent_holes(
        {"members": ["Rails[1]"], "diameter": 12}
    )
    assert "No wall face" in str(result.error)

    no_cut = _Model()
    no_cut.FeatureManager.FeatureCut4 = lambda *a: None
    result = await _Adapter(no_cut).create_vent_holes(
        {"members": ["Rails[1]"], "diameter": 12}
    )
    assert "refused to cut" in str(result.error)

    no_circle = _Model()
    no_circle.SketchManager.CreateCircleByRadius = lambda *a: None
    result = await _Adapter(no_circle).create_vent_holes(
        {"members": ["Rails[1]"], "diameter": 12, "define": "none", "folder": None}
    )
    assert "refused the circle" in str(result.error)


def test_flat_faces_are_read_from_the_body() -> None:
    def vertex(*xyz: float) -> Any:
        return SimpleNamespace(GetPoint=lambda: tuple(c / 1000.0 for c in xyz))

    edge = SimpleNamespace(
        GetStartVertex=lambda: vertex(0, 50, 0),
        GetEndVertex=lambda: vertex(2000, 50, 0),
    )
    flat = SimpleNamespace(
        GetSurface=lambda: SimpleNamespace(
            IsPlane=lambda: True, PlaneParams=(0, 1, 0, 0, 0.05, 0)
        ),
        Normal=(0, 2, 0),
        GetEdges=lambda: (edge,),
        GetArea=lambda: 0.2,
    )
    round_face = SimpleNamespace(
        GetSurface=lambda: SimpleNamespace(IsPlane=lambda: False)
    )
    body = SimpleNamespace(GetFaces=lambda: (flat, round_face))
    adapter = _Adapter(_Model())
    faces = vents._faces(adapter, body)
    assert len(faces) == 1
    face = faces[0]
    assert face.normal == (0.0, 1.0, 0.0)
    assert face.point == (0.0, 50.0, 0.0)
    assert face.area == pytest.approx(200000.0)
    assert face.points == ((0.0, 50.0, 0.0), (2000.0, 50.0, 0.0))


def test_the_wall_is_selected_as_an_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    picked: list[Any] = []

    def face(
        normal: tuple[float, float, float], root: tuple[float, float, float]
    ) -> Any:
        surface = SimpleNamespace(PlaneParams=(*normal, *(c / 1000.0 for c in root)))
        entity = SimpleNamespace(
            Normal=normal,
            GetSurface=lambda: surface,
            Select4=lambda _append, _data: picked.append(normal) or True,
        )
        return entity

    top, side = face((0, 1, 0), (0, 50, 0)), face((0, 0, 1), (0, 0, 50))
    body = SimpleNamespace(GetFaces=lambda: (side, top))
    monkeypatch.setattr(vents, "_bodies", lambda _a, _part: {"Rails[1]": body})
    monkeypatch.setattr(vents, "_as_com", lambda _a, obj, _iface: obj)
    adapter = _Adapter(_Model())
    hole = vents.Hole("vent", (100.0, 50.0, 0.0), (0, 1, 0), 50.0)
    assert vents._select_wall(adapter, adapter.currentModel, "Rails[1]", hole) is True
    assert picked == [(0, 1, 0)]
    off = vents.Hole("vent", (100.0, 60.0, 0.0), (0, 1, 0), 50.0)
    assert vents._select_wall(adapter, adapter.currentModel, "Rails[1]", off) is False
    assert vents._select_wall(adapter, adapter.currentModel, "Gone", hole) is False


def test_a_cut_names_the_body_it_cut() -> None:
    adapter = _Adapter(_Model())
    face = SimpleNamespace(GetBody=lambda: SimpleNamespace(Name="Cut-Extrude7"))
    cut = SimpleNamespace(GetFaces=lambda: (face,))
    assert vents._body_of(adapter, cut) == "Cut-Extrude7"
    assert vents._body_of(adapter, SimpleNamespace(GetFaces=lambda: ())) is None
