"""Tests for weldment profiles: outline geometry and checks, and the build against fakes."""

from __future__ import annotations

import asyncio
import copy
import gc
import hashlib
import json
import math
import os
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
from solidworks_mcp.adapters.solidworks import weldment_profiles as wp
from solidworks_mcp.adapters.solidworks.dialog_watch import DialogSeen
from solidworks_mcp.adapters.solidworks.weldment_profiles import (
    Segment,
    SolidWorksWeldmentProfilesMixin,
    area_and_centroid,
    check_outline,
    check_path,
    extents,
    pierce_points,
)

FIXTURE = (
    Path(__file__).parents[3]
    / "live"
    / "data"
    / "weldment_profiles"
    / "opus_profiles.json"
)


def _catalog() -> list[dict[str, Any]]:
    return list(json.loads(FIXTURE.read_text(encoding="utf-8"))["profiles"])


def _line(a: list[float], b: list[float]) -> dict[str, Any]:
    return {"k": "line", "a": a, "b": b}


def _square(size: float = 10.0, clockwise: bool = False) -> list[dict[str, Any]]:
    """A square centred on the origin, counter-clockwise unless asked."""
    h = size / 2.0
    corners = [[-h, -h], [h, -h], [h, h], [-h, h]]
    if clockwise:
        corners.reverse()
    return [_line(corners[i], corners[(i + 1) % 4]) for i in range(4)]


def _circle(r: float, ccw: bool = True) -> list[dict[str, Any]]:
    """A full circle as two half arcs."""
    a, b = [r, 0.0], [-r, 0.0]
    return [
        {"k": "arc", "a": a, "b": b, "c": [0.0, 0.0], "r": r, "ccw": ccw},
        {"k": "arc", "a": b, "b": a, "c": [0.0, 0.0], "r": r, "ccw": ccw},
    ]


def _numeric(outline: wp.Outline, per_turn: int = 20000) -> tuple[float, float, float]:
    """Area and centroid of the outline drawn as a fine polygon (a cross-check)."""
    area = sx = sy = 0.0
    for loop in outline.loops:
        points = []
        for s in loop:
            if s.kind == "line":
                points.append(s.a)
                continue
            start = math.atan2(s.a[1] - s.c[1], s.a[0] - s.c[0])
            steps = max(8, int(abs(s.sweep()) / math.tau * per_turn))
            for k in range(steps):
                t = start + s.sweep() * k / steps
                points.append((s.c[0] + s.r * math.cos(t), s.c[1] + s.r * math.sin(t)))
        for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1], strict=True):
            cross = x0 * y1 - x1 * y0
            area += cross / 2.0
            sx += (x0 + x1) * cross / 6.0
            sy += (y0 + y1) * cross / 6.0
    return area, sx / area, sy / area


# --- geometry -------------------------------------------------------------


def test_a_square_and_a_ring() -> None:
    square = check_outline({"outer": _square(10.0)})
    ring = check_outline({"outer": _circle(10.0), "holes": [_circle(6.0, ccw=False)]})

    square_area, square_centroid = area_and_centroid(square)
    assert square_area == pytest.approx(100.0)
    assert square_centroid == pytest.approx((0.0, 0.0))
    area, (cx, cy) = area_and_centroid(ring)
    assert area == pytest.approx(math.pi * (100.0 - 36.0))
    assert (cx, cy) == pytest.approx((0.0, 0.0), abs=1e-9)
    assert ring.counts() == {"lines": 0, "arcs": 4}
    assert wp.net_area(ring) == pytest.approx(area)


def test_an_arc_counts_in_the_box_only_where_it_runs() -> None:
    # A quarter arc from (10, 0) to (0, 10) about the origin: its circle reaches
    # x = -10 and y = -10, the arc doesn't.
    quarter = Segment("arc", (10.0, 0.0), (0.0, 10.0), (0.0, 0.0), 10.0, True)
    back = Segment("arc", (0.0, 10.0), (10.0, 0.0), (0.0, 0.0), 10.0, False)
    long_way = Segment("arc", (10.0, 0.0), (0.0, 10.0), (0.0, 0.0), 10.0, False)

    assert quarter.sweep() == pytest.approx(math.pi / 2)
    assert back.sweep() == pytest.approx(-math.pi / 2)
    assert long_way.sweep() == pytest.approx(-3 * math.pi / 2)
    assert extents(((quarter,),)) == pytest.approx((0.0, 0.0, 10.0, 10.0))
    assert extents(((back,),)) == pytest.approx((0.0, 0.0, 10.0, 10.0))
    assert extents(((long_way,),)) == pytest.approx((-10.0, -10.0, 10.0, 10.0))
    assert quarter.mid() == pytest.approx((10 / math.sqrt(2), 10 / math.sqrt(2)))
    assert long_way.mid() == pytest.approx((-10 / math.sqrt(2), -10 / math.sqrt(2)))
    assert Segment("line", (0.0, 0.0), (4.0, 2.0)).mid() == (2.0, 1.0)
    assert Segment("line", (0.0, 0.0), (4.0, 2.0)).sweep() == 0.0


@pytest.mark.parametrize("entry", _catalog(), ids=lambda e: e["path"])
def test_each_catalog_outline_has_its_area(entry: dict[str, Any]) -> None:
    outline = check_outline(entry["outline"])
    area, centroid = area_and_centroid(outline)
    numeric_area, nx, ny = _numeric(outline)

    # The catalog rounds arc ends to 1e-4 mm: its areas agree to 0.001 %.
    assert area == pytest.approx(entry["area_mm2"], rel=1e-5)
    assert area == pytest.approx(numeric_area, rel=1e-5)
    assert centroid == pytest.approx((nx, ny), abs=1e-3)


def test_pierce_points_of_an_angle_and_a_tube() -> None:
    profiles = {p["shape"]: p for p in _catalog()}
    angle = check_outline(profiles["angle"]["outline"])
    tube = check_outline(profiles["rect_tube"]["outline"])

    points = dict(pierce_points(angle))
    assert list(points) == list(wp.PIERCE_POINTS)
    assert points["bottom left"] == (0.0, 0.0)  # the heel
    assert points["top right"] == pytest.approx((40.0, 63.0))
    assert points["right"] == pytest.approx((40.0, 31.5))
    assert points["centroid"] == pytest.approx((9.5216, 20.7789), abs=1e-4)
    # A tube's centroid is its centre: nine points.
    assert [name for name, _ in pierce_points(tube)] == list(wp.PIERCE_POINTS[:-1])


# --- checks ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("x", "isn't a segment object"),
        ({"k": "spline"}, "k must be 'line' or 'arc'"),
        ({"k": "line", "a": [0, 0]}, "a line needs b"),
        ({"k": "line", "a": [0, 0], "b": [1, 0], "r": 1}, "a line has no r"),
        ({"k": "line", "a": [0], "b": [1, 0]}, r"\.a isn't \[x, y\]"),
        ({"k": "line", "a": [0, "x"], "b": [1, 0]}, r"\.a\[1\] isn't a number"),
        ({"k": "line", "a": [True, 0], "b": [1, 0]}, r"\.a\[0\] isn't a number"),
        ({"k": "line", "a": [math.inf, 0], "b": [1, 0]}, "isn't a finite number"),
        ({"k": "line", "a": [0, 0], "b": [0, 0]}, "the line has no length"),
        (
            {"k": "arc", "a": [1, 0], "b": [0, 1], "c": [0, 0], "r": 0, "ccw": True},
            "radius must be positive",
        ),
        (
            {"k": "arc", "a": [1, 0], "b": [0, 1], "c": [0, 0], "r": 1, "ccw": 1},
            "ccw must be true or false",
        ),
        (
            {"k": "arc", "a": [1, 0], "b": [0, 1.1], "c": [0, 0], "r": 1, "ccw": True},
            "end b",
        ),
        (
            {"k": "arc", "a": [1, 0], "b": [1, 0], "c": [0, 0], "r": 1, "ccw": True},
            "give a full circle as two arcs",
        ),
    ],
)
def test_bad_segments_are_refused(raw: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        wp.check_segment(raw, "outer[0]")


def test_a_loop_must_close() -> None:
    gap = _square()
    gap[2]["a"] = [5.0, 5.01]
    with pytest.raises(ValueError, match="at least two segments"):
        wp.check_loop(_square()[:1], "outer")
    with pytest.raises(
        ValueError, match=r"outer\[1\] ends at .* the loop isn't closed"
    ):
        wp.check_loop(gap, "outer")
    # The catalog's rounding (1e-4 mm) is no gap: the start is moved onto the
    # end before it, so the loop SolidWorks draws closes exactly.
    nearly = _square()
    nearly[1]["a"] = [5.0, -5.0001]
    loop = wp.check_loop(nearly, "outer")
    assert len(loop) == 4
    assert loop[1].a == loop[0].b == (5.0, -5.0)
    assert all(loop[i].a == loop[i - 1].b for i in range(4))


def _hole_at(x: float, y: float, size: float = 2.0) -> list[dict[str, Any]]:
    h = size / 2.0
    corners = [[x - h, y - h], [x - h, y + h], [x + h, y + h], [x + h, y - h]]
    return [_line(corners[i], corners[(i + 1) % 4]) for i in range(4)]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "outline must be"),
        ({"outer": _square(), "inner": []}, "outline has no inner"),
        ({"holes": []}, "outline needs outer"),
        ({"outer": _square(), "holes": {}}, "holes must be a list"),
        ({"outer": _square(clockwise=True)}, "the outer loop runs clockwise"),
        (
            {"outer": _square(), "holes": [_square(2.0)]},
            r"holes\[0\] runs counter-clockwise",
        ),
        (
            {"outer": _square(), "holes": [_hole_at(6.0, 0.0)]},
            r"holes\[0\] isn't inside the outer loop",
        ),
        (
            {"outer": _square(), "holes": [_hole_at(0, 0), _hole_at(0.5, 0)]},
            r"holes\[1\] and holes\[0\] overlap",
        ),
        (
            {"outer": _square(), "holes": [_hole_at(0, 0), _hole_at(0, 0, 1.0)]},
            r"holes\[1\] and holes\[0\] overlap",
        ),
    ],
)
def test_bad_outlines_are_refused(raw: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_outline(raw)


def test_a_hole_in_an_angles_empty_corner_is_outside_it() -> None:
    angle = next(p for p in _catalog() if p["shape"] == "angle")
    outline = copy.deepcopy(angle["outline"])
    outline["holes"] = [_hole_at(25.0, 25.0, 10.0)]  # inside the box, not the steel

    with pytest.raises(ValueError, match="isn't inside the outer loop"):
        check_outline(outline)


def test_no_area_left_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wp, "net_area", lambda _outline: 0.0)
    with pytest.raises(ValueError, match="no area is left"):
        check_outline({"outer": _square()})


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Opus/rhs-welded/80x60x2.7.sldlfp", "Opus/rhs-welded/80x60x2.7.sldlfp"),
        ("Opus\\channels\\[10У.SLDLFP", "Opus/channels/[10У.SLDLFP"),
    ],
)
def test_good_paths(path: str, expected: str) -> None:
    assert check_path(path) == expected


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (None, "must be <standard>/<type>/<size>.sldlfp"),
        ("  ", "must be <standard>/<type>/<size>.sldlfp"),
        ("C:/lib/Opus/x/a.sldlfp", "relative to the library root"),
        ("/Opus/x/a.sldlfp", "relative to the library root"),
        ("Opus/a.sldlfp", "SolidWorks reads a library with one file per size"),
        ("Opus/x//a.sldlfp", "SolidWorks reads a library with one file per size"),
        ("Opus/x/a.sldprt", r"must end in <size>\.sldlfp"),
        ("Opus/x/.sldlfp", r"must end in <size>\.sldlfp"),
        ("Opus/../a.sldlfp", "inside the library root"),
        ("Opus/x/a?b.sldlfp", "Windows or SolidWorks refuse"),
        ("Opus/x/a@b.sldlfp", "Windows or SolidWorks refuse"),
        ("Opus/x /a.sldlfp", "starts or ends with a space or a dot"),
        ("Opus/x./a.sldlfp", "starts or ends with a space or a dot"),
        ("Opus/x/a .sldlfp", "starts or ends with a space or a dot"),
        ("Opus/con/a.sldlfp", "a name Windows keeps"),
        ("Opus/x/NUL.sldlfp", "a name Windows keeps"),
    ],
)
def test_bad_paths_are_refused(path: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_path(path)


def test_path_keys_digest_and_case(tmp_path: Path) -> None:
    assert wp.path_key("Opus\\X\\A.sldlfp") == wp.path_key("opus/x/a.SLDLFP")
    capitals = tmp_path / "80x60x2.7.SLDLFP"
    capitals.write_bytes(b"profile")
    target = tmp_path / "80x60x2.7.sldlfp"

    wp.exact_case(str(target))
    wp.exact_case(str(target))  # already right: nothing to do
    wp.exact_case(str(tmp_path / "gone" / "x.sldlfp"))  # best effort: no folder

    assert os.listdir(tmp_path) == ["80x60x2.7.sldlfp"]
    assert wp.digest(str(target)) == (hashlib.sha256(b"profile").hexdigest(), 7)


def test_a_case_rename_refused_still_counts_the_file_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir, library = tmp_path / "work", tmp_path / "lib"
    workdir.mkdir()
    library.mkdir()
    (workdir / "x.SLDLFP").write_bytes(b"new")
    (library / "X.SLDLFP").write_bytes(b"old")
    real = os.replace

    def no_case_rename(src: str, dst: str) -> None:
        if os.path.basename(src) == "X.SLDLFP":
            raise PermissionError("in use")
        real(src, dst)

    monkeypatch.setattr(wp.os, "replace", no_case_rename)

    sha, size = wp._promote(str(workdir), "x.sldlfp", str(library / "x.sldlfp"))

    assert (sha, size) == (hashlib.sha256(b"new").hexdigest(), 3)
    assert (library / "x.sldlfp").read_bytes() == b"new"
    assert len(os.listdir(library)) == 1


# --- the build, against a fake SolidWorks ------------------------------------


class _Feature:
    def __init__(self, name: str, kind: str, specific: Any = None) -> None:
        self.Name, self.kind, self.specific = name, kind, specific
        self.next: _Feature | None = None
        self.selected = 0

    def GetTypeName2(self) -> str:
        return self.kind

    def GetSpecificFeature2(self) -> Any:
        return self.specific

    def GetNextFeature(self) -> Any:
        return self.next

    def Select2(self, _append: bool, _mark: int) -> bool:
        self.selected += 1
        return True


class _Sketch:
    def __init__(self, model: _Model) -> None:
        self.model = model
        self.segments: list[Any] = []
        self.points: list[tuple[float, float]] = []

    def GetSketchSegments(self) -> tuple[Any, ...]:
        return tuple(self.segments)

    def CheckFeatureUse(self, kind: int, opened: Any, closed: Any) -> int:
        self.model.app.checked.append(kind)
        opened.value, closed.value = self.model.app.contours
        return self.model.app.check_status

    def GetConstrainedStatus(self) -> int:
        return 2


class _Manager:
    def __init__(self, model: _Model) -> None:
        self.model = model
        self.ActiveSketch: _Sketch | None = None
        self.AddToDB = False
        self.AutoSolve = True

    def InsertSketch(self, _rebuild: bool) -> None:
        if self.ActiveSketch is None:
            self.ActiveSketch = _Sketch(self.model)
            return
        self.model.sketch = _Feature("Sketch1", "ProfileFeature", self.ActiveSketch)
        self.ActiveSketch = None

    def _segment(self, kind: int) -> Any:
        sketch = self.ActiveSketch
        assert sketch is not None and self.AddToDB
        app = self.model.app
        if app.refuse == "segments":
            return None
        if app.interrupt:
            raise KeyboardInterrupt
        segment = SimpleNamespace(GetType=lambda: kind)
        if app.drop:
            # Made but not kept, as CreateArc did with ends off the circle.
            app.drop = False
            return segment
        sketch.segments.append(segment)
        return segment

    def CreateLine(self, *xyz: float) -> Any:
        return self._segment(0)

    def Create3PointArc(self, *xyz: float) -> Any:
        self.model.app.arcs.append(tuple(v * 1000.0 for v in xyz))
        return self._segment(1)

    def CreatePoint(self, x: float, y: float, _z: float) -> Any:
        sketch = self.ActiveSketch
        assert sketch is not None
        if self.model.app.refuse == "points":
            return None
        sketch.points.append((x * 1000.0, y * 1000.0))
        return object()


class _Properties:
    def __init__(self, model: _Model) -> None:
        self.model = model

    def Add3(self, name: str, _kind: int, value: str, _option: int) -> int:
        if self.model.app.refuse != "properties":
            self.model.properties[name] = value
        return 0

    def Get(self, name: str) -> str:
        return self.model.properties.get(name, "")


class _Extension:
    def __init__(self, model: _Model) -> None:
        self.model = model

    def CustomPropertyManager(self, config: str) -> _Properties:
        assert config == ""
        return _Properties(self.model)

    def SaveAs2(self, path: str, *args: Any) -> bool:
        app = self.model.app
        app.saved.append((path, self.model.sketch.selected if self.model.sketch else 0))
        if app.refuse == "save":
            args[-2].value = 1  # swGenericSaveError
            return False
        # SolidWorks writes the extension in capitals.
        written = path[: -len(".sldlfp")] + ".SLDLFP"
        with open(written, "wb") as handle:
            handle.write(b"SLDLFP " + os.path.basename(path).encode("utf-8"))
        self.model.path = written
        return True


class _Model:
    def __init__(self, app: _App, number: int) -> None:
        self.app, self.title, self.path = app, f"Part{number}", ""
        self.properties: dict[str, str] = {}
        self.sketch: _Feature | None = None
        planes = [_Feature(n, "RefPlane") for n in ("Front", "Top", "Right")]
        for plane, after in zip(planes, planes[1:], strict=False):
            plane.next = after
        self.planes = planes
        self.SketchManager = _Manager(self)
        self.Extension = _Extension(self)

    def GetTitle(self) -> str:
        return os.path.basename(self.path) if self.path else self.title

    def GetPathName(self) -> str:
        return self.path

    def ClearSelection2(self, _all: bool) -> None:
        pass

    def FirstFeature(self) -> Any:
        return self.planes[0]

    def FeatureByPositionReverse(self, _index: int) -> Any:
        return self.sketch


class _App:
    def __init__(self, setting: str = "") -> None:
        self.setting = setting
        self.docs: list[Any] = []
        self.made = 0
        self.closed: list[str] = []
        self.saved: list[tuple[str, int]] = []
        self.arcs: list[tuple[float, ...]] = []
        self.checked: list[int] = []
        self.check_status = 0
        self.contours = (0, 1)
        self.refuse: str | None = None
        self.keep_open = False
        self.new_part_fails = False
        self.drop = False
        self.interrupt = False
        self.toggles = {10: False, 372: False, 520: False, 100: True}

    def NewPart(self) -> Any:
        if self.new_part_fails:
            return None
        return self._new()

    def NewDocument(self, template: str, *_args: Any) -> Any:
        assert template.endswith(".prtdot")
        return self._new()

    def _new(self) -> _Model:
        self.made += 1
        model = _Model(self, self.made)
        self.docs.append(model)
        return model

    def GetDocuments(self) -> tuple[Any, ...]:
        return tuple(self.docs)

    def CloseDoc(self, name: str) -> None:
        self.closed.append(name)
        if self.keep_open:
            return
        self.docs = [
            d for d in self.docs if name not in (d.GetTitle(), d.GetPathName())
        ]

    def GetUserPreferenceStringValue(self, _setting: int) -> str:
        return self.setting

    def GetUserPreferenceToggle(self, toggle: int) -> bool:
        return self.toggles[toggle]

    def SetUserPreferenceToggle(self, toggle: int, value: bool) -> None:
        self.toggles[toggle] = value


class _Adapter(SolidWorksWeldmentProfilesMixin):
    def __init__(self, app: _App | None = None) -> None:
        self.swApp = app or _App()
        self.currentModel: Any = "the user's part"
        self._watch_pid: int | None = None
        self.connected = True
        self.operations: list[str] = []

    def is_connected(self) -> bool:
        return self.connected

    def _attempt(self, fn: Any, default: Any = None) -> Any:
        try:
            return fn()
        except Exception:
            return default

    def _resolve_template_path(self, _slots: list[int], extension: str) -> str:
        return f"C:/templates/Part{extension}"

    def _handle_com_operation(self, name: str, fn: Any) -> AdapterResult[Any]:
        self.operations.append(name)
        try:
            return AdapterResult(status=AdapterResultStatus.SUCCESS, data=fn())
        except Exception as exc:
            return AdapterResult(
                status=AdapterResultStatus.ERROR, error=f"Error in {name}: {exc}"
            )


@pytest.fixture(autouse=True)
def _defined(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """``define_sketch`` stands in: it reports the sketch fully defined."""
    calls: list[Any] = []

    def define(adapter: Any, model: Any, manager: Any, sketch: Any) -> dict[str, Any]:
        calls.append(sketch)
        adapter.swApp.toggles[100] = False  # as its helpers switch the prompt off
        return {"definition": "fully defined"}

    monkeypatch.setattr(wp, "define_sketch", define)
    return calls


def _profile(path: str = "Opus/rhs-welded/80x60x2.7.sldlfp") -> dict[str, Any]:
    entry = next(p for p in _catalog() if p["shape"] == "rect_tube")
    return {
        "path": path,
        "outline": entry["outline"],
        "properties": dict(entry["properties"]),
    }


def _contours(app: _App, profile: dict[str, Any]) -> None:
    """The fake's sketch check finds one closed contour per loop."""
    app.contours = (0, 1 + len(profile["outline"].get("holes") or []))


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.asyncio
async def test_a_profile_is_drawn_checked_saved_closed_and_moved_in(
    tmp_path: Path, _defined: list[Any]
) -> None:
    app = _App()
    adapter = _Adapter(app)
    profile = _profile()
    _contours(app, profile)

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [profile]}
    )

    assert result.is_success, result.error
    [item] = result.data["items"]
    target = tmp_path / "Opus" / "rhs-welded" / "80x60x2.7.sldlfp"
    assert item["status"] == "built" and item["error"] is None
    assert item["file"] == str(target)
    assert item["definition"] == "fully defined"
    assert (item["sha256"], item["bytes"]) == wp.digest(str(target))
    # The spelling asked for, no .part file, nothing else in the library.
    assert _files(tmp_path) == [
        "Opus",
        os.path.join("Opus", "rhs-welded"),
        os.path.join("Opus", "rhs-welded", "80x60x2.7.sldlfp"),
    ]
    assert result.data["root"] == str(tmp_path)
    # Sixteen segments, the arcs through their middles; the sketch checked.
    outline = check_outline(profile["outline"])
    mids = [s.mid() for loop in outline.loops for s in loop if s.kind == "arc"]
    assert [v for a in app.arcs for v in a[6:8]] == pytest.approx(
        [v for m in mids for v in m]
    )
    assert app.checked == [wp._SWEEP_SECTION]
    # Saved in a private folder, the sketch selected, then closed by its path.
    [(staged, selected)] = app.saved
    assert os.path.basename(staged) == "80x60x2.7.sldlfp" and selected == 1
    assert not staged.startswith(str(tmp_path))
    assert app.closed == [staged[: -len(".sldlfp")] + ".SLDLFP"]
    assert not os.path.exists(os.path.dirname(staged))
    assert app.docs == []
    assert adapter.currentModel == "the user's part"
    assert adapter.operations == ["build_weldment_profiles", wp.OPERATION]
    assert app.toggles[100] is True  # put back after Fully Define Sketch
    assert len(_defined) == 1


@pytest.mark.asyncio
async def test_existing_files_are_skipped_unless_overwritten(tmp_path: Path) -> None:
    app = _App()
    adapter = _Adapter(app)
    profile = _profile()
    _contours(app, profile)
    target = tmp_path / "Opus" / "rhs-welded" / "80x60x2.7.sldlfp"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")

    skipped = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [profile]}
    )
    redone = await adapter.build_weldment_profiles(
        {
            "root": str(tmp_path),
            "profiles": [profile],
            "overwrite": True,
            "define": "none",
        }
    )

    assert skipped.data["items"][0]["status"] == "skipped"
    assert skipped.data["items"][0]["sha256"] == hashlib.sha256(b"old").hexdigest()
    assert skipped.data["items"][0]["bytes"] == 3
    built = redone.data["items"][0]
    # Not defined: the sketch's own state, read back.
    assert built["status"] == "built" and built["definition"] == "under defined"
    assert target.read_bytes() == b"SLDLFP 80x60x2.7.sldlfp"


@pytest.mark.parametrize(
    ("refuse", "status", "message"),
    [
        ("segments", 0, "SolidWorks refused the arc from"),
        ("points", 0, "SolidWorks refused the centre pierce point"),
        ("properties", 0, "didn't store the property 'Description'"),
        ("save", 0, "swFileSaveError_e 1"),
        (None, 8, "found an entity of no length"),
        (None, 99, "found status 99"),
    ],
)
@pytest.mark.asyncio
async def test_one_failing_profile_fails_alone_leaves_no_file_and_is_closed(
    tmp_path: Path, refuse: str | None, status: int, message: str
) -> None:
    app = _App()
    adapter = _Adapter(app)
    bad, good = _profile("Opus/a/bad.sldlfp"), _profile("Opus/b/good.sldlfp")
    _contours(app, good)
    app.refuse, app.check_status = refuse, status

    first = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [bad]}
    )
    app.refuse, app.check_status = None, 0
    second = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [good]}
    )

    [failed] = first.data["items"]
    assert failed["status"] == "error", failed
    assert message in failed["error"]
    assert failed["sha256"] is None
    assert second.data["items"][0]["status"] == "built"
    assert app.docs == []  # the failed part was closed too
    assert len(app.closed) == 2
    # The failure left no file and no folder.
    assert not (tmp_path / "Opus" / "a").exists()


@pytest.mark.asyncio
async def test_a_failed_overwrite_keeps_the_old_file(tmp_path: Path) -> None:
    app = _App()
    adapter = _Adapter(app)
    profile = _profile()
    target = tmp_path / "Opus" / "rhs-welded" / "80x60x2.7.sldlfp"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    app.check_status = 8

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [profile], "overwrite": True}
    )

    [item] = result.data["items"]
    assert item["status"] == "error"
    assert item["error"].endswith("; the file there is left as it was")
    assert target.read_bytes() == b"old"


@pytest.mark.asyncio
async def test_a_dropped_segment_or_an_open_contour_fails(tmp_path: Path) -> None:
    app = _App()
    adapter = _Adapter(app)
    profile = _profile()
    app.contours = (1, 1)  # an open contour beside one closed: a gap SolidWorks saw

    open_contour = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [profile]}
    )
    _contours(app, profile)
    app.drop = True
    dropped = await adapter.build_weldment_profiles(
        {"root": str(tmp_path / "b"), "profiles": [profile]}
    )

    assert (
        "1 open and 1 closed contour(s) for 2 loop(s)"
        in (open_contour.data["items"][0]["error"])
    )
    assert "SolidWorks merged or dropped some" in dropped.data["items"][0]["error"]
    assert app.docs == []


@pytest.mark.asyncio
async def test_a_profile_whose_name_is_open_is_refused(tmp_path: Path) -> None:
    app = _App()
    adapter = _Adapter(app)
    other = app._new()
    other.path = str(tmp_path / "elsewhere" / "80X60X2.7.SLDLFP")

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [_profile()]}
    )

    [item] = result.data["items"]
    assert item["status"] == "error"
    assert "is open in SolidWorks; close it" in item["error"]
    assert app.made == 1  # no part was made for it
    assert app.docs == [other]


@pytest.mark.asyncio
async def test_a_saved_part_left_open_is_reported(tmp_path: Path) -> None:
    app = _App()
    app.keep_open, app.new_part_fails = True, True
    adapter = _Adapter(app)
    profile = _profile()
    _contours(app, profile)

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [profile]}
    )

    [item] = result.data["items"]
    assert "SolidWorks didn't close" in item["error"]
    assert "80x60x2.7.SLDLFP; close it (close_model)" in item["error"]
    assert app.made == 1  # made from the default template
    assert not (tmp_path / "Opus").exists()  # not moved in


@pytest.mark.asyncio
async def test_an_unsaved_part_left_open_is_reported(tmp_path: Path) -> None:
    app = _App()
    app.keep_open, app.check_status = True, 8
    adapter = _Adapter(app)

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [_profile()]}
    )

    error = result.data["items"][0]["error"]
    assert "found an entity of no length" in error
    assert "SolidWorks didn't close Part1; close it (close_model)" in error
    assert app.closed == ["Part1"]


@pytest.mark.asyncio
async def test_an_unsaved_part_is_not_closed_by_a_title_another_shows(
    tmp_path: Path,
) -> None:
    app = _App()
    app.check_status = 8
    # Windows hiding known extensions shows a user's Part1.SLDPRT as "Part1".
    users = SimpleNamespace(
        GetTitle=lambda: "Part1", GetPathName=lambda: "C:/work/Part1.SLDPRT"
    )
    app.docs.append(users)
    adapter = _Adapter(app)

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [_profile()]}
    )

    assert app.closed == []
    assert users in app.docs
    assert "SolidWorks didn't close Part1" in result.data["items"][0]["error"]


@pytest.mark.asyncio
async def test_an_interrupt_still_closes_the_part(tmp_path: Path) -> None:
    app = _App()
    app.interrupt = True
    adapter = _Adapter(app)

    with pytest.raises(KeyboardInterrupt):
        await adapter.build_weldment_profiles(
            {"root": str(tmp_path), "profiles": [_profile()]}
        )

    assert app.docs == [] and app.closed == ["Part1"]
    assert adapter.currentModel == "the user's part"
    assert not (tmp_path / "Opus").exists()


@pytest.mark.asyncio
async def test_a_cancelled_call_stops_between_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    adapter = _Adapter(app)
    profiles = [_profile(f"Opus/a/{n}.sldlfp") for n in ("one", "two")]
    _contours(app, profiles[0])

    async def cancelled(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(wp.asyncio, "sleep", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await adapter.build_weldment_profiles(
            {"root": str(tmp_path), "profiles": profiles}
        )

    assert (tmp_path / "Opus" / "a" / "one.sldlfp").is_file()
    assert not (tmp_path / "Opus" / "a" / "two.sldlfp").exists()
    assert app.docs == [] and app.toggles[100] is True


@pytest.mark.asyncio
async def test_a_dialog_the_servers_watch_answered_fails_that_profile_only(
    tmp_path: Path,
) -> None:
    class _Answered(_Adapter):
        def _handle_com_operation(self, name: str, fn: Any) -> AdapterResult[Any]:
            outcome = super()._handle_com_operation(name, fn)
            if name == wp.OPERATION and len(self.operations) == 2:
                return AdapterResult(
                    status=AdapterResultStatus.ERROR,
                    data=outcome.data,
                    error="SolidWorks stopped build_weldment_profile with a dialog: "
                    "'SOLIDWORKS': 'critically low on memory' (answered 'No').",
                )
            return outcome

    app = _App()
    adapter = _Answered(app)
    profiles = [_profile(f"Opus/a/{n}.sldlfp") for n in ("one", "two")]
    _contours(app, profiles[0])

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": profiles}
    )

    first, second = result.data["items"]
    assert first["status"] == "error"
    assert "critically low on memory" in first["error"]
    assert second["status"] == "built"
    assert not (tmp_path / "Opus" / "a" / "one.sldlfp").exists()


@pytest.mark.asyncio
async def test_no_new_part_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    app.new_part_fails = True
    adapter = _Adapter(app)
    monkeypatch.setattr(adapter, "_resolve_template_path", lambda *_a: None)

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [_profile()]}
    )

    assert "Default Templates" in result.data["items"][0]["error"]


@pytest.mark.asyncio
async def test_a_failed_definition_keeps_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any) -> dict[str, Any]:
        raise RuntimeError("no datum")

    monkeypatch.setattr(wp, "define_sketch", broken)
    app = _App()
    adapter = _Adapter(app)
    profile = _profile()
    _contours(app, profile)

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [profile]}
    )

    item = result.data["items"][0]
    assert item["status"] == "built"
    assert item["definition"] == "under defined (not defined: no datum)"


@pytest.mark.asyncio
async def test_a_dialog_left_open_is_refused_and_fails_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[int] = []

    class _Watch:
        def __init__(
            self, pid: int, timeout: float, decline: bool = False, name: str = ""
        ) -> None:
            assert (pid, timeout, decline) == (4242, wp.DIALOG_SECONDS, True)
            self.seen = [
                DialogSeen("SOLIDWORKS", "Rebuild errors", ("Yes", "No"), "No")
            ]

        def __enter__(self) -> _Watch:
            started.append(app.made)  # before the part is made
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

    monkeypatch.setattr(wp, "DialogWatch", _Watch)
    app = _App()
    adapter = _Adapter(app)
    adapter._watch_pid = 4242
    profile = _profile()
    _contours(app, profile)

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [profile]}
    )

    item = result.data["items"][0]
    assert item["status"] == "error"
    assert "stopped build_weldment_profile with a dialog" in item["error"]
    assert "'Rebuild errors' (answered 'No')" in item["error"]
    assert started == [0]
    assert app.docs == []
    assert not (tmp_path / "Opus").exists()


@pytest.mark.asyncio
async def test_a_lost_solidworks_stops_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    adapter = _Adapter(app)

    def gone() -> Any:
        raise RuntimeError(
            "(-2147417848, 'The object invoked has disconnected from its clients.')"
        )

    monkeypatch.setattr(app, "NewPart", gone)
    profiles = [_profile(f"Opus/a/{n}.sldlfp") for n in ("one", "two", "three")]

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": profiles}
    )

    first, *rest = result.data["items"]
    assert "disconnected" in first["error"]
    assert wp.lost_solidworks(first["error"])
    # A connection this process broke fails every later call the same way.
    assert wp.lost_solidworks("(-2147023179, 'The interface is unknown.', None, None)")
    assert not wp.lost_solidworks("SolidWorks refused the arc from [0, 0] to [1, 1]")
    assert [i["error"] for i in rest] == [
        "not built: SolidWorks stopped answering at Opus/a/one.sldlfp; restart it "
        "(and the MCP server) and build again"
    ] * 2


@pytest.mark.asyncio
async def test_the_root_defaults_to_the_first_profile_folder(tmp_path: Path) -> None:
    first = tmp_path / "Library"
    app = _App(setting=f"{first};{tmp_path / 'Other'}")
    adapter = _Adapter(app)
    profile = _profile()
    _contours(app, profile)

    result = await adapter.build_weldment_profiles({"profiles": [profile]})

    assert result.data["root"] == str(first)
    assert (first / "Opus" / "rhs-welded" / "80x60x2.7.sldlfp").is_file()
    assert wp.profiles_root(adapter, "rel") == os.path.abspath("rel")


@pytest.mark.asyncio
async def test_no_root_no_connection_and_a_lost_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unset = await _Adapter(_App(setting=" ; ")).build_weldment_profiles(
        {"profiles": [_profile()]}
    )
    offline = _Adapter()
    offline.connected = False
    gone = await offline.build_weldment_profiles({"profiles": [_profile()]})
    dead = _App()

    def unavailable(_setting: int) -> str:
        raise RuntimeError("(-2147023174, 'The RPC server is unavailable.')")

    monkeypatch.setattr(dead, "GetUserPreferenceStringValue", unavailable)
    lost = await _Adapter(dead).build_weldment_profiles({"profiles": [_profile()]})

    assert "No weldment profile folder: give root" in unset.error
    assert gone.error == "Not connected to SolidWorks"
    # A SolidWorks that is gone says so, not "no folder set".
    assert "RPC server is unavailable" in lost.error


@pytest.mark.asyncio
async def test_a_front_plane_that_cannot_be_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    adapter = _Adapter(app)
    monkeypatch.setattr(_Feature, "Select2", lambda self, *_a: False)

    result = await adapter.build_weldment_profiles(
        {"root": str(tmp_path), "profiles": [_profile()]}
    )

    assert "The Front plane could not be selected" in result.data["items"][0]["error"]
    assert app.docs == []


def test_a_move_that_fails_leaves_the_target_and_no_part_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "x.SLDLFP").write_bytes(b"new")
    target = tmp_path / "lib" / "x.sldlfp"
    target.parent.mkdir()
    target.write_bytes(b"old")

    def refuse(_src: str, _dst: str) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr(wp.os, "replace", refuse)

    with pytest.raises(PermissionError):
        wp._promote(str(workdir), "x.sldlfp", str(target))
    with pytest.raises(Exception, match="SolidWorks wrote no y.sldlfp"):
        wp._promote(str(workdir), "y.sldlfp", str(target))

    assert target.read_bytes() == b"old"
    assert sorted(os.listdir(target.parent)) == ["x.sldlfp"]


def test_a_file_in_no_folder_was_not_written(tmp_path: Path) -> None:
    assert wp._written(str(tmp_path / "none" / "x.sldlfp")) is None
    (tmp_path / "x.SLDLFP").write_bytes(b"")
    assert wp._written(str(tmp_path / "x.sldlfp")) == str(tmp_path / "x.SLDLFP")


def test_a_failed_part_is_let_go_without_a_garbage_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure is kept as text, so no cycle holds the part's dispatches.

    Held in a cycle, a dispatch is released by a later garbage collection,
    perhaps after the adapter's CoUninitialize: one released then broke every
    call of the next connection in the process (RPC_S_UNKNOWN_IF, live).
    """

    class Part:  # a dispatch with no cycles of its own
        def GetTitle(self) -> str:
            return "Part9"

        def GetPathName(self) -> str:
            return ""

    parts = [Part()]
    released = weakref.ref(parts[0])

    def refuse(_adapter: Any, model: Any, _outline: Any, _define: bool) -> Any:
        raise ValueError(f"SolidWorks refused the sketch of {model.GetTitle()}")

    monkeypatch.setattr(wp, "_new_part", lambda _adapter: parts.pop())
    monkeypatch.setattr(wp, "_draw", refuse)
    adapter = _Adapter(_App())
    outline = check_outline(_profile()["outline"])
    gc.disable()
    try:
        with pytest.raises(Exception, match="refused the sketch of Part9") as caught:
            wp._in_solidworks(adapter, str(tmp_path / "x.sldlfp"), outline, {}, False)
        # Even with the exception and its traceback still at hand.
        assert caught.value.__cause__ is None
        assert released() is None
    finally:
        gc.enable()
    assert adapter.swApp.closed == ["Part9"]
