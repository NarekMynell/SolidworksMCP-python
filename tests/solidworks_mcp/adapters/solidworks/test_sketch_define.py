"""Tests for tying generated sketches down, against fakes that keep the selection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from solidworks_mcp.adapters.solidworks import sketch_define as define


class _Point:
    def __init__(self, x: float, y: float, z: float = 0.0, model: Any = None) -> None:
        self.X, self.Y, self.Z = x, y, z
        self.model = model

    def Select4(self, append: bool, _data: Any) -> bool:
        return self.model.select(self, append)


class _Plane:
    def __init__(self, name: str, model: Any) -> None:
        self.Name = name
        self.model = model

    def Select2(self, append: bool, _mark: int) -> bool:
        return self.model.select(self, append)

    def GetTypeName2(self) -> str:
        return "RefPlane"


class _Display:
    def __init__(self, name: str) -> None:
        self.FullName = name

    def GetDimension2(self, _index: int) -> _Display:
        return self


class _Feature:
    def __init__(self, name: str, kind: str, specific: Any = None) -> None:
        self.Name, self.kind, self.specific = name, kind, specific
        self.next: Any = None

    def GetTypeName2(self) -> str:
        return self.kind

    def GetNextFeature(self) -> Any:
        return self.next

    def GetSpecificFeature2(self) -> Any:
        return self.specific


class _Model:
    """A part: three main planes, an origin, a selection, relations and dimensions."""

    def __init__(self, refuse: Any = None) -> None:
        self.selected: list[Any] = []
        self.relations: list[tuple[str, tuple[Any, ...]]] = []
        self.dimensions: dict[str, tuple[Any, ...]] = {}
        self.displays: list[_Display] = []
        self.texts: list[tuple[float, ...]] = []
        self.refuse = refuse or (lambda selected, text: False)
        self.origin = _Point(0.0, 0.0, 0.0, self)
        names = [
            ("Front Plane", "RefPlane"),
            ("Top Plane", "RefPlane"),
            ("Right Plane", "RefPlane"),
        ]
        origin_sketch = SimpleNamespace(GetSketchPoints2=lambda: (self.origin,))
        chain = [_Feature("Origin", "OriginProfileFeature", origin_sketch)]
        self.planes = {n: _Plane(n, self) for n, _k in names}
        chain += [_Feature(n, k) for n, k in names]
        for feature in chain:
            if feature.Name in self.planes:
                feature.Select2 = self.planes[feature.Name].Select2  # type: ignore[attr-defined]
        for a, b in zip(chain, chain[1:], strict=False):
            a.next = b
        self.first = chain[0]
        self.Extension = SimpleNamespace(SelectByID2=self._select_by_id)

    def FirstFeature(self) -> Any:
        return self.first

    def select(self, entity: Any, append: bool) -> bool:
        if not append:
            self.selected = []
        self.selected.append(entity)
        return True

    def ClearSelection2(self, _all: bool) -> None:
        self.selected = []

    def SketchAddConstraints(self, name: str) -> None:
        self.relations.append((name, tuple(self.selected)))

    def _dimension(self, *text: float) -> Any:
        self.texts.append(text)
        if self.refuse(tuple(self.selected), text):
            return None
        name = f"D{len(self.dimensions) + 1}@Sketch"
        self.dimensions[name] = tuple(self.selected)
        self.displays.append(_Display(name))
        return self.displays[-1]

    AddDimension2 = _dimension
    AddHorizontalDimension2 = _dimension
    AddVerticalDimension2 = _dimension

    def _select_by_id(self, name: str, kind: str, *_rest: Any) -> bool:
        if kind == "DIMENSION" and name in self.dimensions:
            self.selected = [name]
            return True
        return False

    def EditDelete(self) -> None:
        for name in self.selected:
            self.dimensions.pop(name, None)


class _Line:
    def __init__(self, a: _Point, b: _Point) -> None:
        self.a, self.b = a, b

    def GetType(self) -> int:
        return 0

    def GetStartPoint2(self) -> _Point:
        return self.a

    def GetEndPoint2(self) -> _Point:
        return self.b

    def Select4(self, append: bool, _data: Any) -> bool:
        return self.a.model.select(self, append)


class _Circle:
    def __init__(self, centre: _Point, radius: float) -> None:
        self.centre, self.radius = centre, radius

    def GetType(self) -> int:
        return 1

    def GetCenterPoint2(self) -> _Point:
        return self.centre

    def GetRadius(self) -> float:
        return self.radius

    def Select4(self, append: bool, _data: Any) -> bool:
        return self.centre.model.select(self, append)


class _Sketch:
    def __init__(
        self, segments: list[Any], points: list[_Point], status: int = 3
    ) -> None:
        self.segments, self.points, self.status = segments, points, status
        self.ModelToSketchTransform = "to-sketch"

    def GetSketchSegments(self) -> list[Any]:
        return self.segments

    def GetSketchPoints2(self) -> list[_Point]:
        return self.points

    def GetConstrainedStatus(self) -> int:
        return self.status


class _Adapter:
    def __init__(self, origin_at: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        self.toggles: dict[int, bool] = {}
        point = SimpleNamespace(
            MultiplyTransform=lambda _t: SimpleNamespace(ArrayData=origin_at)
        )
        utility = SimpleNamespace(CreatePoint=lambda _a: point)
        self.swApp = SimpleNamespace(
            CommandInProgress=False,
            SetUserPreferenceToggle=self.toggles.__setitem__,
            GetMathUtility=lambda: utility,
        )

    def _attempt(self, fn: Any, default: Any = None) -> Any:
        try:
            return fn()
        except Exception:
            return default


def test_definition_names_the_status() -> None:
    assert define.definition(_Sketch([], [], 3)) == "fully defined"
    assert define.definition(_Sketch([], [], 2)) == "under defined"
    assert define.definition(_Sketch([], [], 99)) == "unknown"


def test_quiet_solving_switches_off_then_back_on() -> None:
    adapter = _Adapter()
    manager = SimpleNamespace(AutoSolve=True)
    adapter.swApp.CommandInProgress = False
    with define.quiet_solving(adapter, manager):
        assert manager.AutoSolve is False
        assert adapter.swApp.CommandInProgress is True
    assert manager.AutoSolve is True
    assert adapter.swApp.CommandInProgress is False
    assert adapter.toggles == {10: False, 372: False, 520: False, 100: False}


def test_quiet_solving_turns_solving_on_even_when_it_was_off() -> None:
    adapter = _Adapter()
    manager = SimpleNamespace(AutoSolve=False)
    with define.quiet_solving(adapter, manager):
        pass
    assert manager.AutoSolve is True


def test_main_planes_are_found_by_place_not_name() -> None:
    model = _Model()
    planes = define.main_planes(model)
    assert {axis: p.Name for axis, p in planes.items()} == {
        0: "Right Plane",
        1: "Top Plane",
        2: "Front Plane",
    }


def test_main_planes_missing_is_an_error() -> None:
    model = _Model()
    model.first.next = None
    with pytest.raises(LookupError, match="no Front, Top and Right"):
        define.main_planes(model)


def test_origin_point_and_missing_origin() -> None:
    model = _Model()
    assert define.origin_point(model) is model.origin
    model.first = model.first.next
    with pytest.raises(LookupError, match="no origin"):
        define.origin_point(model)


def test_merge_nodes_merges_ends_that_meet() -> None:
    model = _Model()
    a, b, c = (_Point(0, 0, 0, model), _Point(1, 0, 0, model), _Point(1, 0, 0, model))
    d = _Point(1, 1, 0, model)
    sketch = _Sketch([_Line(a, b), _Line(c, d), _Circle(d, 0.1)], [])
    assert define.merge_nodes(model, sketch) == 1
    assert model.relations == [("sgMERGEPOINTS", (b, c))]


def test_text_places_start_halfway_then_step_aside() -> None:
    places = define.text_places((1.0, 2.0, 3.0), 1)
    assert places[0] == [1.0, 1.0, 3.0]
    assert [1.06, 1.0, 3.0] in [[round(v, 6) for v in p] for p in places]
    assert places[-2:] == [[1.0, 0.5, 3.0], [1.0, 1.5, 3.0]]
    assert len(places) == 7


def test_nodes_get_dimensions_and_relations_to_the_main_planes() -> None:
    model = _Model()
    manager = SimpleNamespace(AutoSolve=True)
    points = [_Point(0.0, 0.0, 0.0, model), _Point(4.0, 3.0, 0.0, model)]
    result = define.tie_nodes_to_planes(_Adapter(), model, manager, _Sketch([], points))
    assert result["nodes"] == 2
    assert result["dimensions"] == 2
    assert result["relations"] == 4
    assert result["fixed"] == 0
    assert result["definition"] == "fully defined"
    planes = {p.Name for (_name, (point, p)) in [(r[0], r[1]) for r in model.relations]}
    assert planes == {"Right Plane", "Top Plane", "Front Plane"}
    # The y text sits halfway between the point and the Top plane.
    assert (4.0, 1.5, 0.0) in model.texts
    # They define the skeleton, not a drawing: none is marked for drawings.
    assert [d.MarkedForDrawing for d in model.displays] == [False, False]


def test_a_refused_dimension_moves_its_text_and_then_takes() -> None:
    refused_once: list[bool] = []

    def refuse(_selected: tuple[Any, ...], text: tuple[float, ...]) -> bool:
        if not refused_once:
            refused_once.append(True)
            return True
        return False

    model = _Model(refuse)
    result = define.tie_nodes_to_planes(
        _Adapter(),
        model,
        SimpleNamespace(AutoSolve=True),
        _Sketch([], [_Point(1.0, 2.0, 3.0, model)]),
    )
    assert result["dimensions"] == 3
    assert result["fixed"] == 0
    assert len(model.texts) == 4


def test_a_node_whose_dimension_is_refused_is_fixed_instead() -> None:
    def refuse_top(selected: tuple[Any, ...], _text: tuple[float, ...]) -> bool:
        return any(getattr(e, "Name", "") == "Top Plane" for e in selected)

    model = _Model(refuse_top)
    point = _Point(4.0, 3.0, 0.5, model)
    result = define.tie_nodes_to_planes(
        _Adapter(), model, SimpleNamespace(AutoSolve=True), _Sketch([], [point])
    )
    assert result["fixed"] == 1
    assert result["fixed_points"] == [[4000.0, 3000.0, 500.0]]
    assert result["dimensions"] == 0
    # Its x dimension came off again, and it got a Fix relation.
    assert model.dimensions == {}
    assert ("sgFIXED", (point,)) in model.relations


def test_origin_in_sketch_reads_the_transform() -> None:
    adapter = _Adapter(origin_at=(0.25, -0.5, 0.0))
    assert define.origin_in_sketch(adapter, _Sketch([], [])) == (0.25, -0.5)


def test_plate_points_are_dimensioned_from_the_origin() -> None:
    model = _Model()
    corner = _Point(0.2, 0.1, 0.0, model)
    on_axis = _Point(0.0, 0.1, 0.0, model)
    centre = _Point(0.15, 0.05, 0.0, model)
    sketch = _Sketch(
        [_Line(corner, on_axis), _Circle(centre, 0.011)], [corner, on_axis]
    )
    result = define.tie_points_to_origin(
        _Adapter(), model, SimpleNamespace(AutoSolve=True), sketch
    )
    # corner: h + v; on_axis: v + a vertical relation; centre (added): h + v; diameter.
    assert result["points"] == 3
    assert result["dimensions"] == 6
    assert result["relations"] == 1
    assert result["fixed"] == 0
    assert result["failed_count"] == 0
    assert ("sgVERTICALPOINTS2D", (on_axis, model.origin)) in model.relations
    # Offsets from the part origin and the diameter stay off drawings.
    assert len(model.displays) == 6
    assert {d.MarkedForDrawing for d in model.displays} == {False}


def test_plate_points_measure_from_where_the_origin_projects() -> None:
    model = _Model()
    point = _Point(0.25, -0.5, 0.0, model)
    result = define.tie_points_to_origin(
        _Adapter(origin_at=(0.25, 0.0, 0.0)),
        model,
        SimpleNamespace(AutoSolve=True),
        _Sketch([], [point]),
    )
    # Its x matches the projected origin's: a relation, not a zero dimension.
    assert result["relations"] == 1
    assert result["dimensions"] == 1
    assert ("sgVERTICALPOINTS2D", (point, model.origin)) in model.relations


def test_a_refused_plate_dimension_fixes_the_point_and_a_refused_diameter_counts() -> (
    None
):
    def refuse_all(_selected: tuple[Any, ...], _text: tuple[float, ...]) -> bool:
        return True

    model = _Model(refuse_all)
    point = _Point(0.2, 0.1, 0.0, model)
    sketch = _Sketch([_Circle(point, 0.01)], [point], status=3)
    result = define.tie_points_to_origin(
        _Adapter(), model, SimpleNamespace(AutoSolve=True), sketch
    )
    assert result["fixed"] == 1
    assert result["failed_count"] == 1
    assert ("sgFIXED", (point,)) in model.relations
