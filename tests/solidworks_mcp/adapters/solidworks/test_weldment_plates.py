"""Tests for weldment plates: their geometry, and the build and DXF flows against fakes."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
from solidworks_mcp.adapters.solidworks import weldment_plates as plates
from solidworks_mcp.adapters.solidworks.weldment_plates import (
    SolidWorksWeldmentPlatesMixin,
    check_plate,
    description,
    main_axis,
    outline_of,
    plate_frame,
)

_BASE = {
    "name": "Base plate",
    "mark": "BP-1",
    "thickness": 15,
    "width": 250,
    "height": 250,
    "origin": [4000, 0, 3000],
    "normal": [0, -1, 0],
    "holes": [{"diameter": 22, "positions": [[-85, -85], [85, 85]]}],
}


def _close(a: Any, b: Any) -> bool:
    return all(math.isclose(x, y, abs_tol=1e-9) for x, y in zip(a, b, strict=True))


# --- geometry -------------------------------------------------------------


def test_a_level_plate_runs_along_model_x() -> None:
    frame = plate_frame([0, 0, 0], [0, -2, 0])
    assert _close(frame.n, (0, -1, 0))
    assert _close(frame.u, (1, 0, 0))
    assert _close(frame.v, (0, 0, 1))
    assert _close(frame.at(10, 20, 5), (10, -5, 20))


def test_a_standing_plate_has_its_y_up() -> None:
    frame = plate_frame([0, 0, 0], [1, 0, 0])
    assert _close(frame.v, (0, 1, 0))
    assert _close(frame.u, (0, 0, -1))


def test_a_given_x_axis_is_projected_onto_the_plate() -> None:
    frame = plate_frame([0, 0, 0], [0, 0, 1], [1, 0, 5])
    assert _close(frame.u, (1, 0, 0))
    assert _close(frame.v, (0, 1, 0))


@pytest.mark.parametrize(
    ("normal", "x_axis", "message"),
    [([0, 0, 0], None, "normal has no length"), ([0, 0, 1], [0, 0, 2], "x_axis")],
)
def test_bad_frames_are_refused(normal: list[float], x_axis: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        plate_frame([0, 0, 0], normal, x_axis)


def test_main_axis_only_for_aligned_normals() -> None:
    assert main_axis((0.0, -1.0, 0.0)) == (1, -1)
    assert main_axis((1.0, 0.0, 0.0)) == (0, 1)
    assert main_axis((0.6, 0.8, 0.0)) is None


def test_outline_and_description() -> None:
    assert outline_of({"width": 200, "height": 100}) == [
        (-100.0, -50.0),
        (100.0, -50.0),
        (100.0, 50.0),
        (-100.0, 50.0),
    ]
    points = check_plate(
        {"name": "G", "thickness": 10, "outline": [[0, 0], [120, 0], [0, 300]]}
    )
    assert description({"thickness": 10}, points) == "PL10 300x120"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"outline": [[0, 0], [0, 0], [10, 10]]}, "repeats the corner"),
        ({"outline": [[0, 0], [10, 0], [20, 0]]}, "no area"),
        (
            {
                "width": 100,
                "height": 100,
                "holes": [{"diameter": 10, "positions": [[80, 0]]}],
            },
            "outside the plate",
        ),
        (
            {
                "width": 100,
                "height": 100,
                "holes": [{"diameter": 20, "positions": [[45, 0]]}],
            },
            "cuts the plate's edge",
        ),
    ],
)
def test_bad_plates_are_refused(spec: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_plate({"name": "P", "mark": "P-1", "thickness": 10, **spec})


def test_slugs_keep_file_names_safe() -> None:
    assert plates._slug("BP-1 Base/plate_t15_x4") == "BP-1_Base_plate_t15_x4"


# --- the build, against a fake part ------------------------------------------


class _Feature:
    def __init__(self, name: str, kind: str, model: Any, specific: Any = None) -> None:
        self.Name, self.kind, self.model, self.specific = name, kind, model, specific

    def GetTypeName2(self) -> str:
        return self.kind

    def GetSpecificFeature2(self) -> Any:
        return self.specific

    def Select2(self, append: bool, _mark: int) -> bool:
        self.model.select(self, append)
        return True


class _Sketch:
    """An open or closed sketch; maps model points by a frame's normal sign."""

    def __init__(self, sign: float = 1.0, segments: list[Any] | None = None) -> None:
        self.sign = sign
        self.segments = segments or []
        self.ModelToSketchTransform = self

    def GetSketchSegments(self) -> list[Any]:
        return self.segments


class _Manager:
    def __init__(self, model: Any) -> None:
        self.model = model
        self.ActiveSketch: Any = None
        self.AddToDB = False
        self.AutoSolve = True
        self.lines: list[tuple[float, ...]] = []
        self.circles: list[tuple[float, ...]] = []
        self.refuse_line = False

    def InsertSketch(self, _rebuild: bool) -> None:
        if self.ActiveSketch is None:
            self.ActiveSketch = _Sketch(self.model.sign)
        else:
            self.model.add(
                f"Sketch{len(self.model.features)}", "ProfileFeature", self.ActiveSketch
            )
            self.ActiveSketch = None

    def Insert3DSketch(self, _rebuild: bool) -> None:
        if self.ActiveSketch is None:
            self.ActiveSketch = _Sketch(segments=[])
        else:
            self.model.add(
                f"3DSketch{len(self.model.features)}",
                "3DProfileFeature",
                self.ActiveSketch,
            )
            self.ActiveSketch = None

    def CreateLine(self, *xyz: float) -> Any:
        if self.refuse_line:
            return None
        self.lines.append(xyz)
        start = SimpleNamespace(
            Select4=lambda append, data: self.model.select("start", append)
        )
        segment = SimpleNamespace(
            GetStartPoint2=lambda: start,
            Select4=lambda append, data: self.model.select("line", append),
        )
        self.ActiveSketch.segments.append(segment)
        return segment

    def CreateCircleByRadius(self, *values: float) -> Any:
        self.circles.append(values)
        return object()


class _Model:
    def __init__(self, sign: float = 1.0) -> None:
        self.sign = sign
        self.features: list[_Feature] = []
        self.selected: list[Any] = []
        self.planes_made: list[tuple[int, float]] = []
        self.extrusions: list[tuple[Any, ...]] = []
        self.bodies: list[Any] = []
        self.hidden: list[str] = []
        self.SketchManager = _Manager(self)
        self.FeatureManager = SimpleNamespace(
            InsertRefPlane=self._plane, FeatureExtrusion3=self._extrude
        )
        self.SelectionManager = SimpleNamespace(
            CreateSelectData=lambda: SimpleNamespace(Mark=-1)
        )
        self.refuse_plane = False

    def add(self, name: str, kind: str, specific: Any = None) -> _Feature:
        feature = _Feature(name, kind, self, specific)
        self.features.append(feature)
        return feature

    def select(self, entity: Any, append: bool) -> bool:
        if not append:
            self.selected = []
        self.selected.append(entity)
        return True

    def ClearSelection2(self, _all: bool) -> None:
        self.selected = []

    def FeatureByPositionReverse(self, _index: int) -> Any:
        return self.features[-1]

    def FeatureByName(self, name: str) -> Any:
        return next((f for f in self.features if f.Name == name), None)

    def BlankRefGeom(self) -> None:
        self.hidden += [getattr(e, "Name", str(e)) for e in self.selected]

    def GraphicsRedraw2(self) -> None:
        pass

    def _plane(self, first: int, distance: float, *_rest: Any) -> Any:
        if self.refuse_plane:
            return None
        self.planes_made.append((first, round(distance, 6)))
        return self.add(f"Plane{len(self.features)}", "RefPlane")

    def _extrude(self, *args: Any) -> Any:
        self.extrusions.append(args)
        self.bodies.append(SimpleNamespace(Name=f"Body{len(self.bodies) + 1}"))
        return self.add(f"Boss-Extrude{len(self.features)}", "Extrusion")

    def GetBodies2(self, _kind: int, _visible: bool) -> list[Any]:
        return list(self.bodies)


class _Adapter(SolidWorksWeldmentPlatesMixin):
    def __init__(self, model: Any) -> None:
        self.currentModel = model
        self.swApp = SimpleNamespace()

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


class _Properties:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})

    def Add3(self, name: str, _kind: int, value: str, _how: int) -> None:
        self.values[name] = value


def _folder(name: str, bodies: list[Any], values: dict[str, str] | None = None) -> Any:
    return SimpleNamespace(
        Name=name,
        GetSpecificFeature2=lambda: SimpleNamespace(GetBodies=lambda: bodies),
        CustomPropertyManager=_Properties(values),
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The plates module with its SolidWorks helpers replaced by recorders."""
    seen: dict[str, Any] = {"folders": [], "tied": 0, "nodes": 0}

    def to_sketch(_adapter: Any, sketch: Any, point_m: list[float]) -> list[float]:
        return [point_m[0], point_m[2], point_m[1] * sketch.sign]

    def into_folder(
        _adapter: Any, _model: Any, folder: str, names: list[str]
    ) -> dict[str, Any]:
        seen["folders"].append((folder, list(names)))
        return {"folder": folder, "moved": list(names), "missing": [], "refused": []}

    def tie_points(*_args: Any) -> dict[str, Any]:
        seen["tied"] += 1
        return {"definition": "fully defined"}

    def tie_nodes(*_args: Any) -> dict[str, Any]:
        seen["nodes"] += 1
        return {"definition": "fully defined"}

    monkeypatch.setattr(plates, "_need_part", lambda _a, _op: None)
    monkeypatch.setattr(plates, "_to_sketch", to_sketch)
    monkeypatch.setattr(plates, "merge_nodes", lambda _m, _s: 0)
    monkeypatch.setattr(plates, "tie_points_to_origin", tie_points)
    monkeypatch.setattr(plates, "tie_nodes_to_planes", tie_nodes)
    monkeypatch.setattr(plates, "move_into_folder", into_folder)
    monkeypatch.setattr(
        plates, "_cut_list_folders", lambda _a, _m, update: seen.get("cut_list", [])
    )
    monkeypatch.setattr(
        plates, "_property", lambda _a, props, name: props.values.get(name, "")
    )
    monkeypatch.setattr(plates, "_as_com", lambda _a, obj, _iface: obj)

    def planes(model: Any) -> dict[int, Any]:
        return {
            axis: model.FeatureByName(name) or model.add(name, "RefPlane")
            for axis, name in ((0, "Right Plane"), (1, "Top Plane"), (2, "Front Plane"))
        }

    monkeypatch.setattr(plates, "main_planes", planes)
    return seen


@pytest.mark.asyncio
async def test_level_plates_share_a_plane_and_are_named_and_described(
    fake: dict[str, Any],
) -> None:
    model = _Model(sign=-1.0)
    first_folder = _folder("Cut-List-Item1", [])
    fake["cut_list"] = [first_folder]
    adapter = _Adapter(model)
    second = {**_BASE, "mark": "BP-2", "origin": [0, 0, 0]}
    raised = {
        **_BASE,
        "mark": "CP-1",
        "name": "Cap",
        "origin": [0, 3040, 0],
        "normal": [0, 1, 0],
    }
    again = {
        **raised,
        "mark": "CP-2",
        "origin": [4000, 3040, 0],
        "thickness_side": "mid",
    }
    result = await adapter.create_plates(
        {"plates": [_BASE, second, raised, again], "folder": "Plates"}
    )
    assert result.status == AdapterResultStatus.SUCCESS, result.error
    data = result.data
    # One plane for both caps (y = 3040); the base plates use Top itself.
    assert data["planes_made"] == 1
    assert model.planes_made == [(8, 3.04)]
    assert [p["plane"] for p in data["plates"]][:2] == ["Top Plane", "Top Plane"]
    assert data["plates"][2]["plane"] == "Plates Y+3040"
    assert "Plates Y+3040" in model.hidden
    assert [p["name"] for p in data["plates"]] == [
        "BP-1 Base plate",
        "BP-2 Base plate",
        "CP-1 Cap",
        "CP-2 Cap",
    ]
    assert data["plates"][0]["description"] == "PL15 250x250"
    assert data["plates"][0]["holes"] == 2
    assert data["plates"][0]["definition"] == "fully defined"
    assert data["plates"][0]["body"] == "BP-1 Base plate"
    # Four corners each, two holes each; one extrusion each.
    assert len(model.SketchManager.lines) == 16
    assert len(model.SketchManager.circles) == 8
    assert len(model.extrusions) == 4
    # Base plates grow down (their normal is -y): sketch normal +... here the
    # fake sketch's normal is reversed, so the base plates are not reversed.
    assert model.extrusions[0][2] is False
    assert model.extrusions[3][3] == plates._END_MID_PLANE
    folder, names = fake["folders"][0]
    assert folder == "Plates"
    assert "Plates Y+3040" in names and "CP-2 Cap" in names
    assert not any(name.endswith(" sketch") for name in names)


@pytest.mark.asyncio
async def test_the_cut_list_item_gets_description_mark_and_thickness(
    fake: dict[str, Any],
) -> None:
    model = _Model()
    member = _folder("RHS80<1>", [SimpleNamespace(Name="Columns[1]")])
    item = _folder("Cut-List-Item2", [SimpleNamespace(Name="BP-1 Base plate")])
    fake["cut_list"] = [member, item]
    result = await _Adapter(model).create_plates({"plates": [_BASE], "folder": None})
    assert result.status == AdapterResultStatus.SUCCESS, result.error
    assert item.CustomPropertyManager.values == {
        "Description": "PL15 250x250",
        "THICKNESS": "15",
        "MARK": "BP-1",
    }
    assert member.CustomPropertyManager.values == {}
    assert result.data["folder"] is None


@pytest.mark.asyncio
async def test_a_tilted_plate_stands_on_a_plane_square_to_its_axis(
    fake: dict[str, Any],
) -> None:
    model = _Model()
    gusset = {
        "name": "Gusset",
        "mark": "GP-1",
        "thickness": 10,
        "outline": [[-100, -60], [100, -60], [100, 60], [-60, 60]],
        "origin": [2000, 1500, 0],
        "normal": [1, 1, 0],
        "holes": [{"diameter": 14, "positions": [[0, 0], [60, 0]]}],
    }
    result = await _Adapter(model).create_plates(
        {"plates": [gusset], "folder": "Plates"}
    )
    assert result.status == AdapterResultStatus.SUCCESS, result.error
    assert result.data["plates"][0]["plane"] == "GP-1 Gusset plane"
    assert model.planes_made == [(plates._REF_PLANE_PERPENDICULAR, 0.0)]
    assert fake["nodes"] == 1
    assert model.FeatureByName("Plate axes") is not None
    assert fake["folders"][0][1][0] == "Plate axes"


@pytest.mark.asyncio
async def test_failures_come_back_as_errors(fake: dict[str, Any]) -> None:
    model = _Model()
    model.SketchManager.ActiveSketch = _Sketch()
    busy = await _Adapter(model).create_plates({"plates": [_BASE]})
    assert "A sketch is open" in str(busy.error)

    refused = _Model()
    refused.refuse_plane = True
    raised = {**_BASE, "origin": [0, 100, 0]}
    result = await _Adapter(refused).create_plates({"plates": [raised]})
    assert "refused the plane" in str(result.error)

    lines = _Model()
    lines.SketchManager.refuse_line = True
    result = await _Adapter(lines).create_plates({"plates": [_BASE]})
    assert "refused an edge" in str(result.error)

    bad = await _Adapter(_Model()).create_plates(
        {"plates": [{**_BASE, "holes": [{"diameter": 22, "positions": [[200, 0]]}]}]}
    )
    assert "outside the plate" in str(bad.error)


# --- DXFs -------------------------------------------------------------------


class _Body:
    def __init__(self, name: str, kind: str, area_m2: float, volume_m3: float) -> None:
        self.Name, self.kind = name, kind
        self.area, self.volume = area_m2, volume_m3

    def GetFeatures(self) -> list[Any]:
        return [SimpleNamespace(GetTypeName2=lambda: self.kind)]

    def GetMassProperties(self, _density: float) -> list[float]:
        return [0.0, 0.0, 0.0, self.volume]


class _Part(_Model):
    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        self.exported: list[str] = []

    def GetPathName(self) -> str:
        return self.path

    def ExportToDWG2(self, path: str, *_rest: Any) -> bool:
        Path(path).write_text("0\nEOF\n", encoding="ascii")
        self.exported.append(path)
        return True


class _DxfAdapter(_Adapter):
    def _largest_flat_face(self, body: Any) -> Any:
        if body.area is None:
            return None
        face = SimpleNamespace(Select4=lambda _append, _data: True)
        return (body.area, face)


@pytest.mark.asyncio
async def test_each_distinct_plate_gets_a_dxf_and_a_line_in_the_list(
    fake: dict[str, Any], tmp_path: Path
) -> None:
    plate = _Body("BP-1 Base plate", "Extrusion", 0.0609795, 0.0609795 * 0.015)
    fake["cut_list"] = [
        _folder("RHS<1>", [_Body("Columns[1]", "WeldMemberFeat", 0.01, 0.001)]),
        _folder("Item2", [plate] * 4, {"MARK": "BP-1", "Description": "PL15 250x250"}),
        _folder("Item3", [_Body("Odd", "Extrusion", None, 0.001)]),  # type: ignore[arg-type]
        _folder("Item4", []),
    ]
    part = _Part(str(tmp_path / "frame.SLDPRT"))
    result = await _DxfAdapter(part).export_plate_dxfs(
        {"output_dir": str(tmp_path / "dxf"), "prefix": "OP-14_"}
    )
    assert result.status == AdapterResultStatus.SUCCESS, result.error
    files = result.data["files"]
    assert [Path(f["file"]).name for f in files] == ["OP-14_BP-1_t15_x4.dxf"]
    assert files[0]["quantity"] == 4
    assert files[0]["thickness_mm"] == 15.0
    assert files[0]["area_mm2"] == 60979.5
    assert result.data["skipped"] == ["Item3"]
    with open(result.data["csv"], encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["file"] == "OP-14_BP-1_t15_x4.dxf"
    assert rows[0]["mark"] == "BP-1"


@pytest.mark.asyncio
async def test_an_unsaved_part_cannot_export_its_plates(
    fake: dict[str, Any], tmp_path: Path
) -> None:
    result = await _DxfAdapter(_Part("")).export_plate_dxfs(
        {"output_dir": str(tmp_path)}
    )
    assert result.status == AdapterResultStatus.ERROR
    assert "never been saved" in str(result.error)


def test_a_sloped_strip_is_described_by_the_sheet_it_is_cut_from() -> None:
    """A fascia strip 3 m along a 17.7° slope is 3032 x 190, not its 2830 x 1105 box."""
    k, run = 0.319788, 2830.0
    strip = [(0.0, 0.0), (-run, -k * run), (-run, -k * run - 200.0), (0.0, -200.0)]
    assert plates.description({"thickness": 2}, strip) == "PL2 3032x190"
    square = [(-125.0, -125.0), (125.0, -125.0), (125.0, 125.0), (-125.0, 125.0)]
    assert plates.smallest_rectangle(square) == pytest.approx((250.0, 250.0))
    # a repeated point is no edge to turn onto
    assert plates.smallest_rectangle(
        [(0.0, 0.0), (0.0, 0.0), (10.0, 0.0), (10.0, 5.0)]
    ) == (
        pytest.approx(10.0),
        pytest.approx(5.0),
    )
