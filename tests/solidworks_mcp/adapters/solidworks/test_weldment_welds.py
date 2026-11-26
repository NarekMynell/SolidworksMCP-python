"""Tests for the shop welds of a weldment (create_weld_beads), against box fakes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from solidworks_mcp.adapters.base import AdapterResultStatus
from solidworks_mcp.adapters.solidworks import weldment_joints as wj
from solidworks_mcp.adapters.solidworks import weldment_welds as ww

from .test_weldment_joints import _A, _Box, _part


def _bodies(*boxes: _Box) -> list[wj.Body]:
    return wj.read_bodies(_A(), _part(*boxes))


def test_a_post_on_its_base_plate_is_welded_from_its_end() -> None:
    post = _Box("Post", (0, 0, 0), (100, 1000, 100))
    plate = _Box("Plate", (-75, -15, -75), (175, 0, 175), hollow=False)

    seams, apart = ww.find_seams(_A(), _bodies(post, plate), ww.REACH_M)

    assert [(s.from_body, s.to_body) for s in seams] == [("Post", "Plate")]
    assert seams[0].face.normal == (0.0, -1.0, 0.0)
    assert seams[0].to_faces == [plate.faces[3]]  # the plate's top
    assert apart == []


def test_a_plate_edge_on_a_member_is_welded_from_the_edge() -> None:
    """Its sides meet the member along one line; its edge lands whole."""
    post = _Box("Post", (0, 0, 0), (100, 1000, 100))
    fin = _Box("Fin", (100, 400, 45), (200, 520, 55), hollow=False)

    seams, apart = ww.find_seams(_A(), _bodies(post, fin), ww.REACH_M)

    assert [(s.from_body, s.to_body) for s in seams] == [("Fin", "Post")]
    assert seams[0].face.normal == (-1.0, 0.0, 0.0)
    assert apart == []


def test_only_the_named_members_joints() -> None:
    post = _Box("Post", (0, 0, 0), (100, 1000, 100))
    plate = _Box("Plate", (-75, -15, -75), (175, 0, 175), hollow=False)
    other = _Box("Other", (500, 0, 0), (600, 1000, 100))
    other_plate = _Box("Other plate", (425, -15, -75), (675, 0, 175), hollow=False)

    seams, _ = ww.find_seams(
        _A(), _bodies(post, plate, other, other_plate), ww.REACH_M, {"Other"}
    )

    assert [(s.from_body, s.to_body) for s in seams] == [("Other", "Other plate")]


def test_a_mitre_is_welded_once() -> None:
    first = SimpleNamespace(IsSame=lambda f: f is end_b)
    end_b = object()
    seams = [ww.Seam("B", "A", wj.Face(end_b, (0,) * 6, True), [object()])]

    assert ww._welded_already(_A(), seams, "B", "A", [first]) is False  # same way round
    assert ww._welded_already(_A(), seams, "A", "B", [first]) is True
    assert (
        ww._welded_already(
            _A(), seams, "A", "B", [SimpleNamespace(IsSame=lambda f: False)]
        )
        is False
    )


class _Loop:
    def __init__(self, outer: bool, edges: list[Any], after: Any = None) -> None:
        self.outer, self.edges, self.after = outer, edges, after

    def IsOuter(self) -> bool:
        return self.outer

    def GetEdges(self) -> list[Any]:
        return self.edges

    def GetNext(self) -> Any:
        return self.after


class _Ref:
    def __init__(self, name: str) -> None:
        self.name = name

    def IsSame(self, other: Any) -> bool:
        return other is self


def test_outer_neighbours_are_across_the_outer_loop_only() -> None:
    end, side1, side2, bore = _Ref("end"), _Ref("s1"), _Ref("s2"), _Ref("bore")
    edge = lambda *faces: SimpleNamespace(GetTwoAdjacentFaces2=lambda: faces)  # noqa: E731
    inner = _Loop(False, [edge(end, bore)])
    outer = _Loop(True, [edge(end, side1), edge(side1, end), edge(end, side2)])
    inner.after = outer
    face = wj.Face(
        SimpleNamespace(GetFirstLoop=lambda: inner, IsSame=end.IsSame), (0,) * 6, True
    )
    face.ref = end
    end.GetFirstLoop = lambda: inner  # type: ignore[attr-defined]

    assert ww.outer_neighbours(_A(), face) == [side1, side2]


class _Model:
    def __init__(self, makes: Any) -> None:
        self.selected: list[tuple[Any, int]] = []
        self.SelectionManager = SimpleNamespace(
            CreateSelectData=lambda: SimpleNamespace(Mark=0)
        )
        self.FeatureManager = SimpleNamespace(
            InsertCosmeticWeldBead=lambda leg: makes(leg)
        )

    def ClearSelection2(self, _all: bool) -> None:
        self.selected = []


def _selectable(model: _Model, name: str, ok: bool = True) -> SimpleNamespace:
    def select(_append: bool, data: Any) -> bool:
        model.selected.append((name, data.Mark))
        return ok

    return SimpleNamespace(Select4=select)


@pytest.fixture
def unflagged(monkeypatch) -> None:
    monkeypatch.setattr(ww._sw_type_info, "flagged", lambda obj, *_i: obj)


def test_a_weld_selects_from_and_to_faces_under_their_marks(
    monkeypatch, unflagged
) -> None:
    legs: list[float] = []
    feature = SimpleNamespace(Name="")
    model = _Model(lambda leg: legs.append(leg) or feature)
    sides = [_selectable(model, "side")]
    monkeypatch.setattr(ww, "outer_neighbours", lambda _a, _f: sides)
    seam = ww.Seam(
        "Post", "Plate", wj.Face(None, (0,) * 6, True), [_selectable(model, "top")]
    )
    seen: list[list[tuple[str, int]]] = []
    real = model.FeatureManager.InsertCosmeticWeldBead
    model.FeatureManager.InsertCosmeticWeldBead = lambda leg: (
        seen.append(list(model.selected)),
        real(leg),
    )[1]

    made, why = ww.weld(_A(), model, seam, 0.0057, "Weld Post to Plate")

    assert made is feature and why is None
    assert feature.Name == "Weld Post to Plate"
    assert seen == [[("side", ww.MARK_FROM), ("top", ww.MARK_TO)]]
    assert legs == [0.0057]


@pytest.mark.parametrize(
    ("sides", "select_ok", "makes", "why"),
    [
        ([], True, None, "no outer edges to weld along"),
        (["side"], False, None, "wouldn't select the faces"),
        (["side"], True, None, "made no weld bead"),
    ],
)
def test_a_weld_that_cant_be_made_says_why(
    monkeypatch, unflagged, sides: list[str], select_ok: bool, makes: Any, why: str
) -> None:
    model = _Model(lambda _leg: makes)
    faces = [_selectable(model, s, select_ok) for s in sides]
    monkeypatch.setattr(ww, "outer_neighbours", lambda _a, _f: faces)
    seam = ww.Seam(
        "Post", "Plate", wj.Face(None, (0,) * 6, True), [_selectable(model, "top")]
    )

    made, reason = ww.weld(_A(), model, seam, 0.004, "Weld")

    assert made is None and why in (reason or "")


class _Welding(ww.SolidWorksWeldmentWeldsMixin, _A):
    """The mixin with a part open and a fake weld folder."""

    def __init__(self) -> None:
        self.currentModel = SimpleNamespace(
            SketchManager=SimpleNamespace(ActiveSketch=None),
            GraphicsRedraw2=lambda: None,
        )

    def _handle_com_operation(self, _name: str, fn: Any) -> Any:
        from solidworks_mcp.adapters.base import AdapterResult

        try:
            return AdapterResult(status=AdapterResultStatus.SUCCESS, data=fn())
        except Exception as exc:  # noqa: BLE001 - the real one does the same
            return AdapterResult(status=AdapterResultStatus.ERROR, error=str(exc))


@pytest.mark.asyncio
async def test_create_weld_beads_reports_each_weld_and_the_total(
    monkeypatch, unflagged
) -> None:
    monkeypatch.setattr(ww, "_need_part", lambda _a, _n: None)
    monkeypatch.setattr(ww, "read_bodies", lambda _a, _p: [])
    seams = [
        ww.Seam("Post", "Plate", wj.Face(None, (0,) * 6, True)),
        ww.Seam("Fin", "Post", wj.Face(None, (0,) * 6, True)),
        ww.Seam("Brace", "Post", wj.Face(None, (0,) * 6, True)),
    ]
    monkeypatch.setattr(ww, "find_seams", lambda *_a: (seams, [("Strap", "Post")]))
    totals = iter([382.8, 642.8])
    made = iter(
        [(object(), None), (object(), None), (None, "SolidWorks made no weld bead")]
    )
    monkeypatch.setattr(ww, "weld", lambda *_a: next(made))
    monkeypatch.setattr(ww, "_weld_length_mm", lambda _a, _f: next(totals))

    result = await _Welding().create_weld_beads({"throat_mm": 4})

    assert result.is_success, result.error
    data = result.data
    assert data["beads"] == 2 and data["leg_mm"] == 5.7
    assert data["welds"] == [
        {"from": "Post", "to": "Plate", "length_mm": 382.8},
        {"from": "Fin", "to": "Post", "length_mm": 260.0},
    ]
    assert data["total_length_mm"] == 642.8
    assert data["failed"] == [
        {"from": "Brace", "to": "Post", "reason": "SolidWorks made no weld bead"}
    ]
    assert data["not_welded"][0]["bodies"] == ["Strap", "Post"]


@pytest.mark.asyncio
async def test_create_weld_beads_refuses_an_open_sketch(monkeypatch) -> None:
    monkeypatch.setattr(ww, "_need_part", lambda _a, _n: None)
    welding = _Welding()
    welding.currentModel.SketchManager.ActiveSketch = object()

    result = await welding.create_weld_beads({"throat_mm": 4})

    assert "A sketch is open" in (result.error or "")


def test_the_weld_folder_length_or_nothing(unflagged) -> None:
    folder = SimpleNamespace(TotalLength=382.83)
    data = SimpleNamespace(GetWeldBeadFolder=lambda: folder)
    feature = SimpleNamespace(GetDefinition=lambda: data)

    assert ww._weld_length_mm(_A(), feature) == 382.8
    assert ww._weld_length_mm(_A(), SimpleNamespace()) is None


@pytest.mark.asyncio
async def test_a_second_call_leaves_welded_joints_and_counts_the_folder(
    monkeypatch, unflagged
) -> None:
    monkeypatch.setattr(ww, "_need_part", lambda _a, _n: None)
    monkeypatch.setattr(ww, "read_bodies", lambda _a, _p: [])
    seams = [
        ww.Seam("Post", "Plate", wj.Face(None, (0,) * 6, True)),
        ww.Seam("Fin", "Post", wj.Face(None, (0,) * 6, True)),
    ]
    monkeypatch.setattr(ww, "find_seams", lambda *_a: (seams, []))
    earlier = SimpleNamespace(Name="Weld Post to Plate")
    monkeypatch.setattr(ww, "bead_features", lambda _a, _m: [earlier])
    totals = {id(earlier): 382.8}
    made = SimpleNamespace(Name="")
    totals[id(made)] = 642.8
    monkeypatch.setattr(ww, "weld", lambda *_a: (made, None))
    monkeypatch.setattr(ww, "_weld_length_mm", lambda _a, f: totals[id(f)])

    result = await _Welding().create_weld_beads({"throat_mm": 4})

    data = result.data
    assert data["welded_already"] == [{"from": "Post", "to": "Plate"}]
    assert data["welds"] == [{"from": "Fin", "to": "Post", "length_mm": 260.0}]
    assert data["total_length_mm"] == 642.8


def test_bead_features_are_found_inside_folders() -> None:
    bead = SimpleNamespace(
        GetTypeName2=lambda: "CosmeticWeldBead",
        Name="Weld A to B",
        GetFirstSubFeature=lambda: None,
        GetNextSubFeature=lambda: None,
    )
    folder = SimpleNamespace(
        GetTypeName2=lambda: "WeldBeadFolder",
        GetFirstSubFeature=lambda: bead,
        GetNextFeature=lambda: None,
        GetNextSubFeature=lambda: None,
    )
    model = SimpleNamespace(FirstFeature=lambda: folder)

    found = ww.bead_features(_A(), model)

    assert [f.Name for f in found] == ["Weld A to B"]
