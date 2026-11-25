"""Tests for the weldment joint check and end caps, on bodies made of boxes.

Each fake body is an axis-aligned box with six planar faces; a face's
closest point is the probe clamped onto its rectangle, and two bodies
interfere where their boxes share volume, which is what SolidWorks answers
for solid boxes.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest

from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
from solidworks_mcp.adapters.solidworks import weldment_joints as wj
from solidworks_mcp.adapters.solidworks import weldments
from solidworks_mcp.adapters.solidworks.weldments import SolidWorksWeldmentsMixin

M = 0.001  # one millimetre in metres


class _Face:
    """One side of a box: a rectangle with an outward normal and a grid tessellation."""

    def __init__(self, lo: tuple, hi: tuple, normal: tuple, body: _Box) -> None:
        self.lo, self.hi, self.normal, self.body = lo, hi, normal, body
        self.selected = False

    def GetBox(self) -> tuple:
        return (*self.lo, *self.hi)

    @property
    def Normal(self) -> tuple:
        return self.normal

    def GetArea(self) -> float:
        sides = [h - lo for lo, h in zip(self.lo, self.hi, strict=True) if h - lo > 0]
        return math.prod(sides)

    def GetLoopCount(self) -> int:
        # A tube's end is a ring: an outer and an inner loop.
        return 2 if self.body.hollow else 1

    def GetTessTriangles(self, _no_conversion: bool) -> tuple:
        axis = next(k for k in range(3) if self.normal[k] != 0)
        u, v = [k for k in range(3) if k != axis]
        out: list[float] = []
        steps = 4
        for i in range(steps):
            for j in range(steps):
                corners = []
                for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1)):
                    point = [0.0, 0.0, 0.0]
                    point[axis] = self.lo[axis]
                    point[u] = self.lo[u] + (self.hi[u] - self.lo[u]) * (i + di) / steps
                    point[v] = self.lo[v] + (self.hi[v] - self.lo[v]) * (j + dj) / steps
                    corners.append(point)
                for tri in ((0, 1, 2), (0, 2, 3)):
                    for c in tri:
                        out.extend(corners[c])
        return tuple(out)

    def GetClosestPointOn(self, x: float, y: float, z: float) -> tuple:
        clamped = [
            min(max(c, lo), hi)
            for c, lo, hi in zip((x, y, z), self.lo, self.hi, strict=True)
        ]
        return (*clamped, 0.0, 0.0)

    def Select4(self, _append: bool, _data: Any) -> bool:
        self.selected = True
        return not self.body.refuse_select


class _Box:
    """A solid box body, given in millimetres."""

    def __init__(
        self,
        name: str,
        lo_mm: tuple,
        hi_mm: tuple,
        refuse_select: bool = False,
        hollow: bool = True,
    ) -> None:
        self.Name = name
        self.lo = tuple(c * M for c in lo_mm)
        self.hi = tuple(c * M for c in hi_mm)
        self.refuse_select = refuse_select
        self.hollow = hollow
        self.faces = []
        for k in range(3):
            for side, sign in ((self.lo, -1.0), (self.hi, 1.0)):
                lo, hi = list(self.lo), list(self.hi)
                lo[k] = hi[k] = side[k]
                normal = [0.0, 0.0, 0.0]
                normal[k] = sign
                self.faces.append(_Face(tuple(lo), tuple(hi), tuple(normal), self))

    def GetBodyBox(self) -> tuple:
        return (*self.lo, *self.hi)

    def GetFaces(self) -> tuple:
        return tuple(self.faces)

    def GetExtremePoint(
        self, x: float, y: float, z: float, ox: Any, oy: Any, oz: Any
    ) -> bool:
        corner = [
            h if d > 0 else lo
            for lo, h, d in zip(self.lo, self.hi, (x, y, z), strict=True)
        ]
        ox.value, oy.value, oz.value = corner
        return True


class _Common:
    """The temporary body two boxes share."""

    def __init__(self, lo: tuple, hi: tuple) -> None:
        self.lo, self.hi = lo, hi

    def GetMassProperties(self, _density: float) -> tuple:
        a, b, c = (h - lo for lo, h in zip(self.lo, self.hi, strict=True))
        centre = tuple((lo + h) / 2 for lo, h in zip(self.lo, self.hi, strict=True))
        return (*centre, a * b * c, 2 * (a * b + b * c + c * a), a * b * c)


class _Modeler:
    def __init__(self) -> None:
        self.asked = 0

    def CheckInterferenceBetweenTwoBodies(
        self, a, b, _coincident, _f1, _f2, common
    ) -> bool:
        self.asked += 1
        lo = tuple(max(p, q) for p, q in zip(a.lo, b.lo, strict=True))
        hi = tuple(min(p, q) for p, q in zip(a.hi, b.hi, strict=True))
        if any(h - lo_ <= 0 for lo_, h in zip(lo, hi, strict=True)):
            return False
        common.value = (_Common(lo, hi),)
        return True


class _Out(SimpleNamespace):
    value: Any = None


class _A:
    def _attempt(self, fn: Any, default: Any = None) -> Any:
        try:
            return fn()
        except Exception:
            return default


def _part(*bodies: _Box) -> SimpleNamespace:
    return SimpleNamespace(GetBodies2=lambda _kind, _visible: tuple(bodies))


def _beam() -> _Box:
    return _Box("Beam", (0, 0, 0), (1000, 40, 40))


def _check(*bodies: _Box, only: set[str] | None = None) -> dict[str, Any]:
    return wj.check(_A(), _part(*bodies), _Modeler(), _Out, 1.0, 0.5, 25, only, _Out)


def test_a_members_size_comes_from_its_extreme_points() -> None:
    bar = _Box("Bar", (0, 0, 0), (1000, 40, 40))
    measured = wj.read_bodies(_A(), _part(bar), _Out)[0]
    wj.read_faces(_A(), measured)
    assert measured.member and wj._span(
        _A(), measured, (1.0, 0.0, 0.0)
    ) == pytest.approx(1.0)
    # an extreme point SolidWorks won't give falls back to the faces' boxes
    bar.GetExtremePoint = lambda *args: False  # type: ignore[method-assign]
    fallback = wj.read_bodies(_A(), _part(bar), _Out)[0]
    wj.read_faces(_A(), fallback)
    assert fallback.member
    assert wj._span(_A(), fallback, (0.0, 1.0, 0.0)) == pytest.approx(0.04)


# --- geometry ---


def test_box_helpers() -> None:
    a, b = (0, 0, 0, 1, 1, 1), (1, 0, 0, 2, 1, 1)
    assert wj.box_overlap_volume(a, b) == 0.0  # touching shares no volume
    assert wj.box_overlap_volume(a, (0.5, 0.5, 0.5, 2, 2, 2)) == pytest.approx(0.125)
    assert wj.boxes_near(a, b, 0.0) and not wj.boxes_near(a, (1.1, 0, 0, 2, 1, 1), 0.05)
    assert wj.in_box((1.02, 0.5, 0.5), a, 0.05) and not wj.in_box(
        (1.2, 0.5, 0.5), a, 0.05
    )
    assert wj.unit((0, 0, 0)) == (0.0, 0.0, 0.0)
    assert wj.mm((0.0012345, 1.0, -0.5)) == [1.2, 1000.0, -500.0]


def test_smallest_eigenvector_and_member_axis() -> None:
    assert wj.smallest_eigenvector([[5, 0, 0], [0, 1, 0], [0, 0, 3]]) in (
        (0, 1, 0),
        (0, -1, 0),
    )
    tilted = wj.smallest_eigenvector([[2, 1, 0], [1, 2, 0], [0, 0, 9]])
    assert abs(abs(tilted[0]) - abs(tilted[1])) < 1e-9 and tilted[0] * tilted[1] < 0
    # a prism along z: big side faces with x and y normals, small z ends
    prism = [
        ((1, 0, 0), 40.0),
        ((-1, 0, 0), 40.0),
        ((0, 1, 0), 40.0),
        ((0, -1, 0), 40.0),
        ((0, 0, 1), 1.0),
        ((0, 0, -1), 1.0),
    ]
    axis = wj.member_axis(prism)
    assert axis is not None and abs(axis[2]) == pytest.approx(1.0)
    # a round tube: only its two ends are planar
    assert wj.member_axis([((0, 1, 0), 5.0), ((0, -1, 0), 5.0)]) == (0.0, 1.0, 0.0)
    assert wj.member_axis([]) is None


def test_samples_spread_over_the_face() -> None:
    face = _Box("B", (0, 0, 0), (100, 100, 10)).faces[5]  # the top, z = 10
    points = wj.triangle_samples(list(face.GetTessTriangles(True)), 4)
    assert len(points) == 4
    assert (
        min(math.dist(p, q) for i, p in enumerate(points) for q in points[i + 1 :])
        > 0.04
    )
    assert wj.triangle_samples([0.0] * 9) == []  # a degenerate triangle gives nothing


def test_groups_join_what_touches() -> None:
    groups = wj.groups_of(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("x", "a")])
    assert groups == [["a", "b", "c"], ["d"]]


# --- the joint check ---


def test_a_post_standing_on_a_beam_is_clean_with_three_open_ends() -> None:
    post = _Box("Post", (480, 40, 0), (520, 1040, 40))

    result = _check(_beam(), post)

    assert result["overlaps"]["count"] == 0
    assert result["clean"] is True
    assert result["loose"] == [] and result["groups"] == 1
    assert result["members"] == 2
    ends = sorted((e["body"], tuple(e["normal"])) for e in result["open_ends"]["ends"])
    assert ends == [
        ("Beam", (-1.0, 0.0, 0.0)),
        ("Beam", (1.0, 0.0, 0.0)),
        ("Post", (0.0, 1.0, 0.0)),
    ]


def test_a_post_buried_in_the_beam_overlaps_and_isnt_called_open() -> None:
    post = _Box("Post", (480, 20, 0), (520, 1040, 40))

    result = _check(_beam(), post)

    pair = result["overlaps"]["pairs"][0]
    assert result["overlaps"]["count"] == 1 and result["clean"] is False
    assert pair["bodies"] == ["Beam", "Post"]
    assert pair["volume_mm3"] == pytest.approx(40 * 20 * 40)
    assert pair["at_mm"] == [500.0, 30.0, 20.0]
    assert [e["body"] for e in result["open_ends"]["ends"]].count(
        "Post"
    ) == 1  # only its top


def test_a_member_touching_nothing_is_loose() -> None:
    far = _Box("Stray", (5000, 0, 0), (6000, 40, 40))

    result = _check(_beam(), _Box("Post", (480, 40, 0), (520, 1040, 40)), far)

    assert result["loose"] == ["Stray"] and result["loose_count"] == 1
    assert result["groups"] == 2 and result["group_sizes"] == [2, 1]
    assert result["clean"] is False


def test_an_overlap_a_hair_deep_is_touching_however_large() -> None:
    """Flats laid face to face on a slope overlapped 3.6 mm³ a thousandth deep (seen live)."""
    under = _Box("Under", (0, 0, 0), (100, 10, 100))
    sliver = _Box("Over", (0, 9.999, 0), (100, 20, 100))  # 10 mm³, 0.001 mm deep
    sunk = _Box("Sunk", (0, 9.9, 0), (100, 20, 100))  # 1000 mm³, 0.1 mm deep

    assert _check(under, sliver)["overlaps"]["count"] == 0
    assert _check(under, sunk)["overlaps"]["pairs"][0]["volume_mm3"] == 1000.0


def test_a_deep_lump_beside_a_thin_sliver_is_still_an_overlap() -> None:
    sliver = _Common((0.0, 0.0, 0.0), (1.0, 0.000001, 0.05))  # 50 mm³, a micron deep
    chunk = _Common((0.0, 0.0, 0.0), (0.005, 0.005, 0.005))  # 125 mm³, 5 mm deep

    class TwoLumps(_Modeler):
        def CheckInterferenceBetweenTwoBodies(self, a, b, _c, _f1, _f2, common) -> bool:
            common.value = (sliver, chunk)
            return True

    a = _Box("A", (0, 0, 0), (1000, 10, 50))
    b = _Box("B", (0, 9, 0), (1000, 20, 50))
    bodies = wj.read_bodies(_A(), _part(a, b), _Out)

    pairs, _ = wj.overlaps(_A(), TwoLumps(), bodies, 1e-9, _Out)

    assert pairs[0]["volume_mm3"] == 125.0  # the sliver doesn't count, the chunk does


def test_an_end_hanging_mostly_off_its_support_is_partly_open() -> None:
    hanging = _Box("Post", (995, 40, 0), (1035, 1040, 40))  # an eighth on the beam
    half = _Box("Half", (980, 40, 100), (1020, 1040, 140))  # misses the beam: 100 < z

    result = _check(_beam(), hanging)

    partly = result["partly_open_ends"]["ends"]
    assert result["partly_open_ends"]["count"] == 1 and partly[0]["body"] == "Post"
    assert 0 < partly[0]["covered_percent"] < 100 * wj.CLOSED_SHARE
    assert result["clean"] is False
    assert set(_check(_beam(), half)["loose"]) == {"Beam", "Half"}  # apart, both loose


def test_a_plate_under_a_post_closes_its_end_and_isnt_a_member() -> None:
    plate = _Box("Plate", (400, -15, -80), (600, 0, 120))
    post = _Box("Post", (480, 0, 0), (520, 1000, 40))

    result = _check(plate, post)

    assert result["members"] == 1
    assert [e["body"] for e in result["open_ends"]["ends"]] == ["Post"]  # only its top
    assert result["groups"] == 1 and result["clean"]


def test_a_strap_or_a_lapped_member_touching_only_with_its_side_is_joined() -> None:
    post = _Box("Post", (480, 0, 0), (520, 1000, 40))
    # 16 x 60 x 240: long enough to count as a member, its ends in the air
    strap = _Box("Strap", (520, 400, -10), (536, 640, 50), hollow=False)
    rail = _Box("Rail", (300, 1000, 0), (700, 1040, 40))  # lapped across the top
    lapped = _Box("Lapped", (480, 1040, -500), (520, 1080, 500))  # on the rail's side

    result = _check(post, strap, rail, lapped)

    assert result["members"] == 4
    assert result["groups"] == 1 and result["loose"] == []
    # checking only the strap joins it to the post too
    assert _check(post, strap, only={"Strap"})["loose"] == []


def test_checking_some_members_only() -> None:
    post = _Box("Post", (480, 20, 0), (520, 1040, 40))
    stray = _Box("Stray", (5000, 0, 0), (6000, 40, 40))
    modeler = _Modeler()

    result = wj.check(
        _A(), _part(_beam(), post, stray), modeler, _Out, 1.0, 0.5, 25, {"Stray"}
    )

    assert result["checked_bodies"] == 1
    assert (
        result["overlaps"]["count"] == 0 and modeler.asked == 0
    )  # the beam-post pair isn't asked
    assert result["loose"] == ["Stray"]
    assert {e["body"] for e in result["open_ends"]["ends"]} == {"Stray"}


def test_one_body_is_never_loose_and_unreadable_bodies_are_skipped() -> None:
    broken = _Box("Broken", (0, 0, 0), (10, 10, 10))
    broken.GetBodyBox = lambda: ()  # type: ignore[method-assign]
    assert _check(_beam())["loose"] == []
    assert wj.read_bodies(_A(), _part(broken)) == []


def test_faces_without_a_box_or_a_normal() -> None:
    body = _Box("Odd", (0, 0, 0), (1000, 40, 40))
    body.faces[0].GetBox = lambda: None  # type: ignore[method-assign]
    curved = body.faces[1]
    type(curved).Normal = property(  # type: ignore[assignment]
        lambda self: (0.0, 0.0, 0.0) if self is curved else self.normal
    )
    try:
        bodies = wj.read_bodies(_A(), _part(body))
        faces = wj.read_faces(_A(), bodies[0])
        assert len(faces) == 5 and not faces[0].planar
        assert wj.read_faces(_A(), bodies[0]) is faces  # cached
    finally:
        type(curved).Normal = property(lambda self: self.normal)  # type: ignore[assignment]


def test_a_point_picks_the_flush_end_it_is_in() -> None:
    left = _Box("Left", (0, 0, 0), (1000, 30, 30))
    right = _Box("Right", (0, 0, 40), (1000, 30, 70))  # flush, side by side
    bodies = wj.read_bodies(_A(), _part(left, right), _Out)

    chosen, missed = wj.end_faces_at(_A(), bodies, [[1000.0, 15.0, 55.0]], 20.0)

    assert missed == [] and len(chosen) == 1
    assert chosen[0].box[2] == pytest.approx(0.04)  # Right's end, not Left's


def test_end_faces_at_points() -> None:
    bodies = wj.read_bodies(
        _A(), _part(_beam(), _Box("Post", (480, 40, 0), (520, 1040, 40)))
    )

    faces, missed = wj.end_faces_at(
        _A(), bodies, [[500, 1045, 20], [-3, 20, 20], [500, 5000, 0]], 20.0
    )

    assert [f.normal for f in faces] == [(0.0, 1.0, 0.0), (-1.0, 0.0, 0.0)]
    assert missed == [[500, 5000, 0]]


# --- the adapter's check_joints and create_end_caps ---


class _CapManager:
    def __init__(self, part: _CapPart) -> None:
        self.part = part
        self.calls: list[tuple] = []

    def InsertEndCapFeature3(self, *args: Any) -> Any:
        self.calls.append(args)
        if self.part.refuse:
            return None
        made = self.part.make_caps
        for face in [f for b in self.part.bodies for f in b.faces if f.selected][:made]:
            lo, hi = list(face.lo), list(face.hi)
            axis = next(k for k in range(3) if face.normal[k] != 0)
            hi[axis] = lo[axis] + args[0] * face.normal[axis]
            lo_mm = tuple(min(p, q) / M for p, q in zip(lo, hi, strict=True))
            hi_mm = tuple(max(p, q) / M for p, q in zip(lo, hi, strict=True))
            self.part.bodies.append(
                _Box(f"End cap1[{len(self.part.bodies)}]", lo_mm, hi_mm)
            )
        return SimpleNamespace(Name="End cap1")


class _CapPart:
    def __init__(self, *bodies: _Box) -> None:
        self.bodies = list(bodies)
        self.refuse = False
        self.make_caps = 99
        self.FeatureManager = _CapManager(self)
        self.cleared = 0

    def GetBodies2(self, _kind: int, _visible: bool) -> tuple:
        return tuple(self.bodies)

    def ClearSelection2(self, _all: bool) -> None:
        self.cleared += 1

    def FeatureByName(self, name: str) -> Any:
        return None

    def FirstFeature(self) -> Any:
        return None


class _Adapter(SolidWorksWeldmentsMixin):
    def __init__(self, part: Any, modeler: Any = None) -> None:
        self.currentModel = part
        self.swApp = SimpleNamespace(GetModeler=lambda: modeler)
        self.doc_type = 1

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
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weldments, "_doc_type", lambda a: a.doc_type)
    monkeypatch.setattr(weldments, "_byref_variant", _Out)


@pytest.mark.asyncio
async def test_check_joints_through_the_adapter() -> None:
    part = _CapPart(_beam(), _Box("Post", (480, 20, 0), (520, 1040, 40)))
    adapter = _Adapter(part, _Modeler())

    whole = await adapter.check_joints({})
    no_modeler = await _Adapter(part).check_joints({})
    unknown = await adapter.check_joints({"members": ["Purlins"]})
    drawing = _Adapter(part, _Modeler())
    drawing.doc_type = 3
    wrong = await drawing.check_joints({})

    assert whole.is_success and whole.data["overlaps"]["count"] == 1
    assert "no modeler" in (no_modeler.error or "")
    assert "No structural member or body named 'Purlins'" in (unknown.error or "")
    assert "check_joints needs an open part" in (wrong.error or "")


@pytest.mark.asyncio
async def test_end_caps_on_every_open_end() -> None:
    part = _CapPart(_beam(), _Box("Post", (480, 40, 0), (520, 1040, 40)))
    adapter = _Adapter(part, _Modeler())

    capped = await adapter.create_end_caps(
        {
            "open_ends": True,
            "thickness_mm": 6,
            "placement": "outward",
            "inset_mm": 2,
            "name": "Caps",
        }
    )
    again = await adapter.create_end_caps({"open_ends": True})

    assert capped.is_success, capped.error
    data = capped.data
    assert data["caps"] == 3 and len(data["ends"]) == 3 and "warning" not in data
    assert data["feature"] == "Caps"
    args = part.FeatureManager.calls[0]
    assert args[0] == pytest.approx(0.006) and args[3] == pytest.approx(0.002)
    assert args[-1] == wj.CAP_PLACEMENT["outward"]
    assert part.cleared >= 1
    assert not again.is_success and "No open member end to cap" in (again.error or "")


@pytest.mark.asyncio
async def test_a_coped_end_in_the_air_is_named_not_capped() -> None:
    bar = _coped(_Box("Bar", (0, 0, 0), (1000, 40, 40)), 1)  # its +x end coped
    part = _CapPart(bar)
    adapter = _Adapter(part, _Modeler())

    capped = await adapter.create_end_caps({"open_ends": True})

    assert capped.is_success, capped.error
    assert capped.data["caps"] == 1  # the flat end
    assert [e["normal"] for e in capped.data["not_capped"]] == [[1.0, 0.0, 0.0]]
    assert "1 coped end(s) touching nothing, not capped (Bar)" in capped.data["warning"]


@pytest.mark.asyncio
async def test_end_caps_at_points_and_their_refusals() -> None:
    def fresh() -> _CapPart:
        return _CapPart(_beam(), _Box("Post", (480, 40, 0), (520, 1040, 40)))

    part = fresh()
    at_points = await _Adapter(part).create_end_caps(
        {"ends": [[500, 1040, 20], [500, 1041, 21]]}
    )
    missed = await _Adapter(fresh()).create_end_caps({"ends": [[0, 5000, 0]]})
    nothing = await _Adapter(fresh()).create_end_caps({"ends": []})
    stubborn = fresh()
    stubborn.bodies[1].refuse_select = True
    unselected = await _Adapter(stubborn).create_end_caps({"ends": [[500, 1040, 20]]})
    refused = fresh()
    refused.refuse = True
    no_feature = await _Adapter(refused).create_end_caps({"ends": [[500, 1040, 20]]})
    short = fresh()
    short.make_caps = 0
    fewer = await _Adapter(short).create_end_caps({"ends": [[500, 1040, 20]]})

    assert at_points.is_success and at_points.data["caps"] == 1  # two points, one end
    assert at_points.data["ends"][0]["body"] == "Post"
    assert at_points.data["placement"] == "inward"
    assert "No member end within 20 mm" in (missed.error or "")
    assert "No member end found" in (nothing.error or "")
    assert "selected 0 of the 1 end faces" in (unselected.error or "")
    assert "refused the end caps" in (no_feature.error or "")
    assert (
        fewer.is_success
        and "1 end(s) selected but 0 cap body(ies) made" in fewer.data["warning"]
    )


@pytest.mark.asyncio
async def test_end_caps_need_a_part() -> None:
    adapter = _Adapter(_CapPart())
    adapter.doc_type = 2
    result = await adapter.create_end_caps({"open_ends": True})
    assert "create_end_caps needs an open part" in (result.error or "")


def test_an_open_section_s_free_end_needs_no_cap() -> None:
    """An I-beam or angle end touching nothing is free, not an open tube end."""
    beam = _Box("Beam", (0, 0, 0), (1000, 40, 40), hollow=False)

    result = _check(beam)

    assert result["open_ends"]["count"] == 0
    assert result["free_ends"]["count"] == 2


def test_a_tube_end_half_on_a_beam_is_partly_open() -> None:
    """A post whose top only a narrow bar covers: its ring touches, its bore is open."""
    post = _Box("Post", (0, 0, 0), (100, 1000, 100))
    bar = _Box("Bar", (-500, 1000, 40), (600, 1040, 60))  # 20 mm wide, across the top

    result = _check(post, bar)

    partly = [e for e in result["partly_open_ends"]["ends"] if e["body"] == "Post"]
    assert partly and partly[0]["hollow"] is True
    assert result["clean"] is False


def test_a_tube_end_fully_under_a_plate_is_closed() -> None:
    post = _Box("Post", (0, 0, 0), (100, 1000, 100))
    plate = _Box("Cap", (-20, 1000, -20), (120, 1010, 120), hollow=False)

    result = _check(post, plate)

    assert all(
        e["body"] != "Post" or e["normal"][1] < 0 for e in result["open_ends"]["ends"]
    )
    assert not [e for e in result["partly_open_ends"]["ends"] if e["body"] == "Post"]


def test_an_end_is_found_from_a_point_in_its_bore() -> None:
    """The middle of a tube's end lies on its plane, far from the ring: still that end."""
    post = _Box("Post", (0, 0, 0), (100, 1000, 100))
    bodies = wj.read_bodies(_A(), _part(post), _Out)

    found, missed = wj.end_faces_at(
        _A(), bodies, [[50, 1000, 50], [50, 1060, 50]], 20.0
    )

    assert len(found) == 1 and found[0].normal == (0.0, 1.0, 0.0)
    assert missed == [[50, 1060, 50]]


def test_tubes_mitred_or_butted_end_to_end_are_closed() -> None:
    """Two tubes whose rings meet whole: their bores join, nothing is open there."""
    lower = _Box("Lower", (0, 0, 0), (100, 1000, 100))
    upper = _Box("Upper", (0, 1000, 0), (100, 2000, 100))
    result = _check(lower, upper)
    at_joint = [
        e
        for e in result["open_ends"]["ends"] + result["partly_open_ends"]["ends"]
        if 900 < e["at_mm"][1] < 1100
    ]
    assert at_joint == []


# --- coped (curved) ends ---


class _Curved(_Face):
    """A face SolidWorks gives no single normal (a coped saddle); flat here, normals per vertex."""

    @property
    def Normal(self) -> tuple:
        return (0.0, 0.0, 0.0)

    def GetTessNorms(self) -> tuple:
        return tuple(self.normal) * (len(self.GetTessTriangles(True)) // 3)


def _coped(box: _Box, index: int) -> _Box:
    """``box`` with face ``index`` (0 -x, 1 +x, 2 -y, 3 +y, 4 -z, 5 +z) made curved."""
    face = box.faces[index]
    box.faces[index] = _Curved(face.lo, face.hi, face.normal, box)
    return box


def test_a_coped_end_is_one_of_the_members_ends() -> None:
    bar = _coped(_Box("Bar", (0, 0, 0), (1000, 40, 40)), 1)
    body = wj.read_bodies(_A(), _part(bar), _Out)[0]
    wj.read_faces(_A(), body)
    assert body.member and len(body.ends) == 2
    assert sorted(tuple(f.normal) for f in body.ends) == [
        (-1.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
    ]

    low = _coped(_Box("Low", (0, 0, 0), (1000, 40, 40)), 0)
    low.faces.append(_Curved((0.4, 0.0, 0.0), (0.45, 0.0, 0.04), (0.0, -1.0, 0.0), low))
    body = wj.read_bodies(_A(), _part(low), _Out)[0]
    wj.read_faces(_A(), body)
    # the -x saddle points out of the low end; a curved patch mid-length isn't an end
    assert sorted(tuple(f.normal) for f in body.ends) == [
        (-1.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
    ]

    # a bolt hole near the end reaches neither the tip nor across the section
    strip = _Box("Strip", (0, 0, 0), (3000, 365, 2))
    strip.faces.append(
        _Curved((2.95, 0.1, 0.0), (2.968, 0.118, 0.002), (0.0, 0.0, 1.0), strip)
    )
    body = wj.read_bodies(_A(), _part(strip), _Out)[0]
    wj.read_faces(_A(), body)
    assert len(body.ends) == 2
    # without extreme points the faces' boxes give the tip
    bare = _coped(_Box("Bare", (0, 0, 0), (1000, 40, 40)), 1)
    body = wj.read_bodies(_A(), _part(bare), None)[0]
    wj.read_faces(_A(), body)
    assert len(body.ends) == 2

    side = _coped(_Box("Side", (0, 0, 0), (1000, 40, 40)), 2)
    body = wj.read_bodies(_A(), _part(side), _Out)[0]
    wj.read_faces(_A(), body)
    assert len(body.ends) == 2  # a curved side along the whole member is no end


def test_a_round_tube_s_side_on_a_diagonal_is_no_end() -> None:
    """Measured by its box's two corners, a diagonal tube's side looked short."""
    face = SimpleNamespace(box=(0.0, 0.0, 0.0, 1.0, 1.0, 0.04), planar=False)
    body = SimpleNamespace(axis=wj.unit((1.0, -1.0, 0.0)))

    assert wj._coped_ends(_A(), body, [face], 1.414, 0.04) == []


def test_a_coped_end_in_the_air_is_open() -> None:
    result = _check(_coped(_Box("Bar", (0, 0, 0), (1000, 40, 40)), 1))
    normals = sorted(tuple(e["normal"]) for e in result["open_ends"]["ends"])
    assert normals == [(-1.0, 0.0, 0.0), (1.0, 0.0, 0.0)]


def test_a_stub_touching_only_through_its_curved_face_is_joined() -> None:
    """A standoff coped onto a round handle touched it only there (seen live: 3 groups)."""
    frame = _Box("Frame", (-500, 0, -40), (500, 20, 0))
    stub = _coped(_Box("Stub", (0, 0, 0), (20, 20, 40)), 5)
    handle = _Box("Handle", (-500, 0, 40), (500, 20, 60))

    result = _check(frame, stub, handle)

    assert result["groups"] == 1 and result["loose"] == []


def test_a_face_is_probed_where_its_box_meets_the_body() -> None:
    def faces(box: _Box) -> list[wj.Face]:
        return wj.read_faces(_A(), wj.read_bodies(_A(), _part(box), _Out)[0])

    post = wj.read_bodies(
        _A(), _part(_Box("Post", (480, 0, 0), (520, 1000, 40))), _Out
    )[0]
    reach = 0.5 * M
    strap = faces(_Box("Strap", (520, 400, -10), (536, 640, 50)))
    assert wj._face_touches(_A(), strap[0], post, reach)  # its -x face on the post
    assert not wj._face_touches(_A(), strap[1], post, reach)  # +x: the boxes don't meet
    gap = faces(_Box("Gap", (521, 400, -10), (537, 640, 50)))
    assert not wj._face_touches(_A(), gap[0], post, reach)  # 1 mm off
    refused = _Box("Refused", (520, 400, -10), (536, 640, 50))
    refused.faces[0].GetClosestPointOn = None  # type: ignore[assignment,method-assign]
    assert not wj._face_touches(_A(), faces(refused)[0], post, reach)
    curved = faces(_coped(_Box("Curved", (520, 400, -10), (536, 640, 50)), 0))
    assert wj._face_touches(_A(), curved[0], post, reach)  # along its own normals
    blind = _coped(_Box("Blind", (520, 400, -10), (536, 640, 50)), 0)
    blind.faces[0].GetTessNorms = lambda: ()  # type: ignore[method-assign]
    assert not wj._face_touches(_A(), faces(blind)[0], post, reach)  # no way out


def test_a_round_bar_lapped_on_a_flat_face_is_joined_but_not_one_hung_off() -> None:
    post = _Box("Post", (480, 0, 0), (520, 1000, 40))
    rail = _Box("Rail", (300, 1000, 0), (700, 1040, 40))
    bar = _Box("Handle", (480, 1040, -500), (520, 1080, 500), hollow=False)
    for index in (0, 1, 2, 3):  # round: every side curved, its ends flat
        _coped(bar, index)

    assert _check(post, rail, bar)["groups"] == 1  # found from the rail's top face

    # a leaf hung 3 mm off its post isn't joined to it, whatever contact_mm says
    leaf = _Box("Leaf", (523, 0, 0), (563, 1000, 40))
    hung = wj.check(_A(), _part(post, leaf), _Modeler(), _Out, 1.0, 5.0, 25, None, _Out)
    assert hung["groups"] == 2


def test_normals_at_falls_back_without_vertex_normals() -> None:
    tri = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    assert wj._normals_at(tri, [], [(0.3, 0.3, 0.0)], (0.0, 0.0, 1.0)) == [
        (0.0, 0.0, 1.0)
    ]
    # vertex normals that cancel give no direction either
    assert wj._normals_at(tri, [0.0] * 9, [(0.3, 0.3, 0.0)], (1.0, 0.0, 0.0)) == [
        (1.0, 0.0, 0.0)
    ]
    assert wj._normals_at(
        tri, [0.0, 0.0, 2.0] * 3, [(0.3, 0.3, 0.0)], (1.0, 0.0, 0.0)
    ) == [(0.0, 0.0, 1.0)]
