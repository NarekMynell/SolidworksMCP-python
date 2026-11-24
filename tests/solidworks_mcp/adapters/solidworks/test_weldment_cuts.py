"""Tests for cuts in weldment members: their checks, and the build against the plate tests' fake part."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
from solidworks_mcp.adapters.solidworks import weldment_cuts as cuts
from solidworks_mcp.adapters.solidworks import weldment_plates as plates
from solidworks_mcp.adapters.solidworks.weldment_cuts import (
    SolidWorksWeldmentCutsMixin,
    check_cut,
)

from .test_weldment_plates import _Model, _Sketch

# The plate tests' fixture, registered here under this name: cuts draw with
# the plates' sketch and plane helpers, so they share its recorders.
from .test_weldment_plates import fake as plate_fake  # noqa: F401

#: A joist end at x = 2000 (its web on the Front plane): a cope through the
#: top flange, two Ø18 holes in the web below it.
_COPE = {
    "name": "FJ-1 end cope",
    "origin": [2000, 2920, 0],
    "normal": [0, 0, 1],
    "outline": [[-80, 0], [10, 0], [10, 90], [-80, 90]],
    "holes": [{"diameter": 18, "positions": [[-40, -40], [-40, -95]]}],
    "depth": 120,
}


def test_a_cut_needs_an_outline_or_holes() -> None:
    with pytest.raises(ValueError, match="give an outline"):
        check_cut({"name": "nothing", "depth": 10})


def test_holes_alone_have_no_outline() -> None:
    holes = {"name": "web holes", "holes": [{"diameter": 18, "positions": [[0, 0]]}]}

    assert check_cut(holes) == []


def test_an_opening_is_a_centred_rectangle() -> None:
    assert check_cut({"name": "slot", "width": 40, "height": 20}) == [
        (-20.0, -10.0),
        (20.0, -10.0),
        (20.0, 10.0),
        (-20.0, 10.0),
    ]


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"outline": [[0, 0], [0, 0], [10, 10]]}, "repeats the corner"),
        ({"outline": [[0, 0], [10, 0], [20, 0]]}, "has no area"),
        (
            {
                "outline": [[0, 0], [100, 0], [100, 100], [0, 100]],
                "holes": [{"diameter": 18, "positions": [[50, 50]]}],
            },
            "inside the outline",
        ),
        (
            {"holes": [{"diameter": 18, "positions": [[0, 0], [10, 0]]}]},
            "overlap",
        ),
    ],
    ids=["repeated corner", "no area", "hole inside", "holes overlap"],
)
def test_bad_cuts_are_refused(spec: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_cut({"name": "cut", **spec})


class _CutModel(_Model):
    """The plates' fake part, cutting: each cut goes through the bodies it lists."""

    def __init__(self, cut_bodies: list[str] | None) -> None:
        super().__init__()
        self.cuts: list[tuple[Any, ...]] = []
        self.cut_bodies = cut_bodies
        self.FeatureManager.FeatureCut4 = self._cut

    def _cut(self, *args: Any) -> Any:
        self.cuts.append(args)
        if self.cut_bodies is None:
            return None
        faces = [
            SimpleNamespace(GetBody=lambda name=name: SimpleNamespace(Name=name))
            for name in self.cut_bodies
        ]
        feature = self.add(f"Cut-Extrude{len(self.features)}", "Cut")
        feature.GetFaces = lambda: faces  # type: ignore[attr-defined]
        return feature


class _Adapter(SolidWorksWeldmentCutsMixin):
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


@pytest.fixture
def fake(
    plate_fake: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """The plates' recorders, plus this module's own references to them."""
    monkeypatch.setattr(cuts, "_need_part", lambda _a, _op: None)
    monkeypatch.setattr(cuts, "main_planes", plates.main_planes)
    monkeypatch.setattr(cuts, "move_into_folder", plates.move_into_folder)
    return plate_fake


@pytest.mark.asyncio
async def test_a_cope_and_its_holes_are_one_cut_through_the_depth(
    fake: dict[str, Any],
) -> None:
    model = _CutModel(["IPE160 joist", "FP-1 fin plate", "IPE160 joist"])

    result = await _Adapter(model).create_cuts({"cuts": [_COPE], "folder": "Cuts"})

    assert result.status == AdapterResultStatus.SUCCESS, result.error
    made = result.data["cuts"][0]
    assert made["name"] == made["feature"] == "FJ-1 end cope"
    assert made["sketch"] == "FJ-1 end cope sketch"
    assert made["holes"] == 2
    assert made["bodies"] == ["FP-1 fin plate", "IPE160 joist"]
    assert made["definition"] == "fully defined"
    # On the Front plane itself (the web's centre at z = 0): no plane made.
    assert made["plane"] == "Front Plane"
    assert result.data["planes_made"] == 0
    # Four outline edges, two holes, then one cut: mid-plane, 120 mm through.
    assert len(model.SketchManager.lines) == 4
    assert len(model.SketchManager.circles) == 2
    (cut,) = model.cuts
    assert cut[3] == cuts._END_MID_PLANE
    assert cut[5] == pytest.approx(0.12)
    assert fake["folders"] == [("Cuts", ["FJ-1 end cope"])]


@pytest.mark.asyncio
async def test_cuts_on_one_level_share_a_plane_named_for_cuts(
    fake: dict[str, Any],
) -> None:
    model = _CutModel(["IPE240 beam"])
    flange = {
        "name": "MB-1 flange holes",
        "origin": [0, 2760, 0],
        "normal": [0, 1, 0],
        "holes": [{"diameter": 18, "positions": [[-45, -35], [45, 35]]}],
        "depth": 40,
    }
    other = {**flange, "name": "MB-1 flange holes 2", "origin": [6000, 2760, 0]}

    result = await _Adapter(model).create_cuts({"cuts": [flange, other]})

    assert result.status == AdapterResultStatus.SUCCESS, result.error
    assert [c["plane"] for c in result.data["cuts"]] == ["Cuts Y+2760"] * 2
    assert result.data["planes_made"] == 1
    assert "Cuts Y+2760" in model.hidden
    assert result.data["folder"] is None


@pytest.mark.asyncio
async def test_a_tilted_cut_stands_on_its_own_plane_from_the_cut_axes(
    fake: dict[str, Any],
) -> None:
    model = _CutModel(["Brace"])
    slot = {
        "name": "Brace slot",
        "origin": [1000, 1000, 0],
        "normal": [1, 1, 0],
        "width": 60,
        "height": 20,
        "depth": 30,
    }

    result = await _Adapter(model).create_cuts({"cuts": [slot], "folder": "Cuts"})

    assert result.status == AdapterResultStatus.SUCCESS, result.error
    assert result.data["cuts"][0]["plane"] == "Brace slot plane"
    assert model.FeatureByName("Cut axes") is not None
    assert fake["folders"][0][1][0] == "Cut axes"


@pytest.mark.asyncio
async def test_a_cut_that_reaches_no_steel_says_how_to_place_it(
    fake: dict[str, Any],
) -> None:
    result = await _Adapter(_CutModel(None)).create_cuts({"cuts": [_COPE]})

    assert result.status == AdapterResultStatus.ERROR
    assert "no steel within 120 mm of its plane" in str(result.error)


@pytest.mark.asyncio
async def test_an_open_sketch_or_a_bad_cut_is_refused(fake: dict[str, Any]) -> None:
    model = _CutModel(["Beam"])
    model.SketchManager.ActiveSketch = _Sketch()
    busy = await _Adapter(model).create_cuts({"cuts": [_COPE]})
    assert "A sketch is open" in str(busy.error)

    bad = await _Adapter(_CutModel(["Beam"])).create_cuts(
        {"cuts": [{"name": "empty", "origin": [0, 0, 0], "normal": [0, 0, 1]}]}
    )
    assert "give an outline" in str(bad.error)
