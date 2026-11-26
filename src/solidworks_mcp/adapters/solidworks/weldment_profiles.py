"""Weldment profiles from exact section outlines, many in one call.

A catalog section (a tube, an angle, a channel, a bar) comes as its outline:
an outer loop running counter-clockwise and holes running clockwise, each a
chain of lines and arcs in millimetres around the profile's own origin (a
tube centred on it, an angle's heel on it, a channel on y = 0). Each profile
becomes a library feature part, ``<root>/<standard>/<type>/<size>.sldlfp``,
the layout of a library with one file per size: one sketch on the Front
plane with exactly those lines and true arcs, sketch points to pierce at
(the box centre, its corners and edge midpoints, and the area centroid), and
the custom properties a cut list takes over.

Checked live on SolidWorks 2025 SP4 before it was written (COM pitfall #48;
``tests/live/test_live_sw_weldment_profiles.py`` proves the rest):

- ``ISketchManager::CreateArc(centre, start, end, direction)`` dropped an
  arc whose ends were off its circle by the catalog's rounding (1e-4 mm):
  a channel's two root arcs were missing, though the call returned an
  object for each, and the sketch check said "zero-length entity".
  ``Create3PointArc(start, end, mid)`` keeps the ends exactly where the
  neighbouring lines start and end, so every loop closes; the centre moves
  by the rounding (1.2e-4 mm on that channel).
- ``IModelDocExtension::SaveAs2`` to a ``.sldlfp`` path, silent, saves the
  new part as a library feature part (``LibFeat1`` with the sketch under it)
  and writes the extension in capitals. A profile is saved in a private
  folder and copied into the library once it was saved and closed, so a
  failure leaves the library as it was.
- A file of one configuration goes into ``InsertStructuralWeldment5`` with
  an empty configuration; it is refused under the configuration's name,
  ``Default`` or another (:meth:`.SolidWorksWeldmentsMixin._resolve_profile`).
- ``IModelDoc2::Close`` is not implemented (E_NOTIMPL); ``CloseDoc`` with the
  full path closed the right one of two documents of one name, where
  ``CloseDoc`` with the name closed the other.
- Fully Define Sketch (``sketch_define.define_sketch``) leaves some outlines
  under defined (an I-beam, with its pierce points or without);
  ``SketchAddConstraints("sgFIXED")`` on every entity at once added nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import shutil
import tempfile
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any, cast

from .. import sw_type_info as _sw_type_info
from ..base import AdapterResult, AdapterResultStatus
from .com_utils import (
    SAVE_AS_CURRENT_VERSION,
    SAVE_AS_SILENT,
    as_list,
    pause_after_document,
    strip_op_prefix,
)
from .dialog_watch import DialogWatch, refused_message
from .features import _null_callout
from .io import _byref_int
from .sketch_define import define_sketch, main_planes, solve_on
from .sketch_define import definition as sketch_definition

#: A point of an outline, mm.
Point = tuple[float, float]

#: Consecutive segments share an end within this, mm (a catalog rounds to 1e-4).
JOIN_MM = 1e-3
#: An arc's ends lie this close to its circle, mm.
ON_ARC_MM = 1e-3
#: Segments shorter than this are refused, mm.
MIN_SEGMENT_MM = 1e-3
#: Pierce points this close are one point, mm.
SAME_POINT_MM = 1e-6
#: The suffix of a profile file.
SUFFIX = ".sldlfp"
#: Characters Windows or SolidWorks refuse in a file or folder name.
_REFUSED_CHARACTERS = set('<>:"|?*@')
#: Names Windows keeps for devices.
_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
#: The pierce points every profile gets, in this order.
PIERCE_POINTS = (
    "centre",
    "bottom left",
    "bottom right",
    "top right",
    "top left",
    "bottom",
    "right",
    "top",
    "left",
    "centroid",
)

_TAU = 2.0 * math.pi


@dataclass(frozen=True)
class Segment:
    """One line or arc of an outline loop, mm.

    Attributes:
        kind: ``line`` or ``arc``.
        a: The start point.
        b: The end point.
        c: An arc's centre; the origin for a line.
        r: An arc's radius; 0 for a line.
        ccw: An arc runs counter-clockwise from ``a`` to ``b``.
    """

    kind: str
    a: Point
    b: Point
    c: Point = (0.0, 0.0)
    r: float = 0.0
    ccw: bool = True

    def _angles(self) -> tuple[float, float]:
        """The angles of ``a`` and ``b`` about the centre, radians."""
        return (
            math.atan2(self.a[1] - self.c[1], self.a[0] - self.c[0]),
            math.atan2(self.b[1] - self.c[1], self.b[0] - self.c[0]),
        )

    def sweep(self) -> float:
        """An arc's signed sweep, radians: positive counter-clockwise, 0 for a line."""
        if self.kind != "arc":
            return 0.0
        start, end = self._angles()
        if self.ccw:
            return (end - start) % _TAU
        return -((start - end) % _TAU)

    def mid(self) -> Point:
        """The point halfway along the segment (along an arc's sweep)."""
        if self.kind != "arc":
            return (self.a[0] + self.b[0]) / 2.0, (self.a[1] + self.b[1]) / 2.0
        angle = self._angles()[0] + self.sweep() / 2.0
        return (
            self.c[0] + self.r * math.cos(angle),
            self.c[1] + self.r * math.sin(angle),
        )

    def extremes(self) -> list[Point]:
        """Its ends and, for an arc, the points of its sweep furthest along x or y.

        A bounding box from these is exact: an arc counts only where it runs,
        never as its whole circle.
        """
        points = [self.a, self.b]
        if self.kind != "arc":
            return points
        start, _ = self._angles()
        sweep = self.sweep()
        for quarter in range(4):
            angle = quarter * math.pi / 2.0
            along = (angle - start) % _TAU if sweep > 0 else (start - angle) % _TAU
            if along < abs(sweep):
                points.append(
                    (
                        self.c[0] + self.r * math.cos(angle),
                        self.c[1] + self.r * math.sin(angle),
                    )
                )
        return points

    def terms(self) -> tuple[float, float, float]:
        """Its share of a loop's area and first moments, by Green's theorem.

        Returns:
            tuple[float, float, float]: ``(A, Sy, Sx)``: the area
            ``∮ (x dy - y dx) / 2``, the moment ``∮ x² dy / 2`` (the area
            times the centroid's x) and ``-∮ y² dx / 2`` (times its y), mm.
        """
        if self.kind != "arc":
            (x0, y0), (x1, y1) = self.a, self.b
            return (
                (x0 * y1 - x1 * y0) / 2.0,
                (y1 - y0) * (x0 * x0 + x0 * x1 + x1 * x1) / 6.0,
                -(x1 - x0) * (y0 * y0 + y0 * y1 + y1 * y1) / 6.0,
            )
        cx, cy = self.c
        r = self.r
        t0 = self._angles()[0]
        t1 = t0 + self.sweep()

        def between(f: Any) -> float:
            return float(f(t1) - f(t0))

        sines, cosines = between(math.sin), between(math.cos)
        area = (cx * r * sines - cy * r * cosines + r * r * (t1 - t0)) / 2.0
        cos2 = between(lambda t: t / 2.0 + math.sin(2.0 * t) / 4.0)
        cos3 = between(lambda t: math.sin(t) - math.sin(t) ** 3 / 3.0)
        sin2 = between(lambda t: t / 2.0 - math.sin(2.0 * t) / 4.0)
        sin3 = between(lambda t: -math.cos(t) + math.cos(t) ** 3 / 3.0)
        moment_y = (r / 2.0) * (cx * cx * sines + 2.0 * cx * r * cos2 + r * r * cos3)
        moment_x = (r / 2.0) * (-cy * cy * cosines + 2.0 * cy * r * sin2 + r * r * sin3)
        return area, moment_y, moment_x


#: A closed chain of segments.
Loop = tuple[Segment, ...]


@dataclass(frozen=True)
class Outline:
    """A profile's section: the outer loop and its holes.

    Attributes:
        outer: The outer loop, counter-clockwise.
        holes: The holes, each clockwise.
    """

    outer: Loop
    holes: tuple[Loop, ...]

    @property
    def loops(self) -> tuple[Loop, ...]:
        """Every loop, the outer one first."""
        return (self.outer, *self.holes)

    def counts(self) -> dict[str, int]:
        """How many lines and arcs the outline has."""
        segments = [s for loop in self.loops for s in loop]
        arcs = sum(1 for s in segments if s.kind == "arc")
        return {"lines": len(segments) - arcs, "arcs": arcs}


def loop_terms(loop: Loop) -> tuple[float, float, float]:
    """A loop's signed area and first moments (see :meth:`Segment.terms`)."""
    area = moment_y = moment_x = 0.0
    for segment in loop:
        a, my, mx = segment.terms()
        area, moment_y, moment_x = area + a, moment_y + my, moment_x + mx
    return area, moment_y, moment_x


def net_area(outline: Outline) -> float:
    """The section's area, mm²: the outer loop's less the holes'."""
    return sum(loop_terms(loop)[0] for loop in outline.loops)


def area_and_centroid(outline: Outline) -> tuple[float, Point]:
    """The section's area, mm², and its area centroid, mm.

    The holes run clockwise, so their negative areas take their steel away.
    """
    area = moment_y = moment_x = 0.0
    for loop in outline.loops:
        a, my, mx = loop_terms(loop)
        area, moment_y, moment_x = area + a, moment_y + my, moment_x + mx
    return area, (moment_y / area, moment_x / area)


def extents(loops: tuple[Loop, ...]) -> tuple[float, float, float, float]:
    """The box around the loops, ``(x_min, y_min, x_max, y_max)`` mm, from the arcs' sweeps."""
    points = [p for loop in loops for s in loop for p in s.extremes()]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def pierce_points(outline: Outline) -> list[tuple[str, Point]]:
    """The points a structural member can be pierced at, named as in ``PIERCE_POINTS``.

    The centre of the outline's box, its four corners and four edge
    midpoints, and the area centroid. Points that coincide (a tube's centre
    and centroid) are one point under the first name.
    """
    x0, y0, x1, y1 = extents((outline.outer,))
    xm, ym = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    _, centroid = area_and_centroid(outline)
    candidates: list[tuple[str, Point]] = [
        ("centre", (xm, ym)),
        ("bottom left", (x0, y0)),
        ("bottom right", (x1, y0)),
        ("top right", (x1, y1)),
        ("top left", (x0, y1)),
        ("bottom", (xm, y0)),
        ("right", (x1, ym)),
        ("top", (xm, y1)),
        ("left", (x0, ym)),
        ("centroid", centroid),
    ]
    kept: list[tuple[str, Point]] = []
    for name, point in candidates:
        if all(math.dist(point, p) > SAME_POINT_MM for _, p in kept):
            kept.append((name, point))
    return kept


def _number(value: Any, where: str) -> float:
    """A finite number, or ``ValueError`` naming ``where``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} isn't a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{where} isn't a finite number")
    return number


def _point(value: Any, where: str) -> Point:
    """A point ``[x, y]``, mm."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{where} isn't [x, y]")
    return _number(value[0], f"{where}[0]"), _number(value[1], f"{where}[1]")


_LINE_KEYS = {"k", "a", "b"}
_ARC_KEYS = {"k", "a", "b", "c", "r", "ccw"}


def check_segment(raw: Any, where: str) -> Segment:
    """One segment of a loop, checked.

    Args:
        raw: ``{"k": "line", "a": [x, y], "b": [x, y]}`` or ``{"k": "arc",
            "a", "b", "c": [cx, cy], "r": radius, "ccw": true|false}``, mm.
        where: Where it is, for the message, e.g. ``outer[3]``.

    Returns:
        Segment: The segment.

    Raises:
        ValueError: A key missing or unknown, a value of the wrong kind, a
            segment of no length, an arc whose ends are off its circle, or an
            arc that ends where it starts.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{where} isn't a segment object")
    kind = raw.get("k")
    if kind not in ("line", "arc"):
        raise ValueError(f"{where}: k must be 'line' or 'arc', not {kind!r}")
    wanted = _LINE_KEYS if kind == "line" else _ARC_KEYS
    missing, unknown = sorted(wanted - set(raw)), sorted(set(raw) - wanted)
    if missing:
        raise ValueError(f"{where}: a {kind} needs {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{where}: a {kind} has no {', '.join(unknown)}")
    a, b = _point(raw["a"], f"{where}.a"), _point(raw["b"], f"{where}.b")
    if kind == "line":
        if math.dist(a, b) < MIN_SEGMENT_MM:
            raise ValueError(f"{where}: the line has no length")
        return Segment("line", a, b)
    c = _point(raw["c"], f"{where}.c")
    r = _number(raw["r"], f"{where}.r")
    if r < MIN_SEGMENT_MM:
        raise ValueError(f"{where}: the arc's radius must be positive")
    if not isinstance(raw["ccw"], bool):
        raise ValueError(f"{where}.ccw must be true or false")
    for name, end in (("a", a), ("b", b)):
        off = abs(math.dist(end, c) - r)
        if off > ON_ARC_MM:
            raise ValueError(
                f"{where}: the arc's end {name} {list(end)} is {off:.4g} mm off its "
                f"circle (centre {list(c)}, radius {r:g})"
            )
    if math.dist(a, b) < MIN_SEGMENT_MM:
        raise ValueError(
            f"{where}: the arc ends where it starts; give a full circle as two arcs"
        )
    return Segment("arc", a, b, c, r, raw["ccw"])


def check_loop(raw: Any, where: str) -> Loop:
    """A closed loop: segments each starting where the one before ends.

    A segment's start within ``JOIN_MM`` of the end before is moved onto that
    end, so the loop SolidWorks draws closes exactly (each segment is drawn
    from its own coordinates).

    Raises:
        ValueError: Fewer than two segments, a bad segment, or a gap (the
            message names the segments and how far apart they are).
    """
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError(f"{where} must be a list of at least two segments")
    loop = tuple(check_segment(s, f"{where}[{i}]") for i, s in enumerate(raw))
    for index, segment in enumerate(loop):
        following = loop[(index + 1) % len(loop)]
        gap = math.dist(segment.b, following.a)
        if gap > JOIN_MM:
            raise ValueError(
                f"{where}[{index}] ends at {list(segment.b)} but "
                f"{where}[{(index + 1) % len(loop)}] starts at {list(following.a)} "
                f"({gap:.4g} mm apart): the loop isn't closed"
            )
    return tuple(
        replace(segment, a=loop[index - 1].b) for index, segment in enumerate(loop)
    )


#: Degrees of arc per side when a loop is drawn as a polygon to test points in.
_POLYGON_STEP_DEG = 2.0


def polygon(loop: Loop) -> list[Point]:
    """The loop as a polygon: its ends, and its arcs in steps of a few degrees."""
    points: list[Point] = []
    for segment in loop:
        points.append(segment.a)
        if segment.kind != "arc":
            continue
        start, sweep = segment._angles()[0], segment.sweep()
        steps = max(2, math.ceil(abs(math.degrees(sweep)) / _POLYGON_STEP_DEG))
        for k in range(1, steps):
            angle = start + sweep * k / steps
            points.append(
                (
                    segment.c[0] + segment.r * math.cos(angle),
                    segment.c[1] + segment.r * math.sin(angle),
                )
            )
    return points


def inside(point: Point, shape: list[Point]) -> bool:
    """Whether ``point`` lies inside the polygon ``shape`` (even-odd rule)."""
    x, y = point
    within = False
    for (x1, y1), (x2, y2) in zip(shape, shape[1:] + shape[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            within = not within
    return within


def _samples(loop: Loop) -> list[Point]:
    """Points along a loop: each segment's middle."""
    return [segment.mid() for segment in loop]


def check_outline(raw: Any) -> Outline:
    """A profile's outline, checked: closed loops, the outer one counter-clockwise.

    Args:
        raw: ``{"outer": [segment, ...], "holes": [[segment, ...], ...]}``, mm.

    Returns:
        Outline: The outline.

    Raises:
        ValueError: An unknown key, a bad loop, an outer loop running
            clockwise, a hole running counter-clockwise, outside the outer
            loop or inside another hole, or no area left. Loops that cross
            themselves or each other are left to SolidWorks' own check.
    """
    if not isinstance(raw, dict):
        raise ValueError("outline must be {outer: [...], holes: [[...], ...]}")
    unknown = sorted(set(raw) - {"outer", "holes"})
    if unknown:
        raise ValueError(f"outline has no {', '.join(unknown)}")
    if "outer" not in raw:
        raise ValueError("outline needs outer")
    holes_raw = raw.get("holes")
    if holes_raw is None:
        holes_raw = []
    if not isinstance(holes_raw, list):
        raise ValueError("outline.holes must be a list of loops")
    outer = check_loop(raw["outer"], "outer")
    if loop_terms(outer)[0] <= 0:
        raise ValueError("the outer loop runs clockwise; give it counter-clockwise")
    outer_shape = polygon(outer)
    holes: list[Loop] = []
    for index, hole_raw in enumerate(holes_raw):
        where = f"holes[{index}]"
        hole = check_loop(hole_raw, where)
        if loop_terms(hole)[0] >= 0:
            raise ValueError(f"{where} runs counter-clockwise; give holes clockwise")
        if not all(inside(p, outer_shape) for p in _samples(hole)):
            raise ValueError(f"{where} isn't inside the outer loop")
        for other, earlier in enumerate(holes):
            shape = polygon(earlier)
            if any(inside(p, shape) for p in _samples(hole)) or any(
                inside(p, polygon(hole)) for p in _samples(earlier)
            ):
                raise ValueError(f"{where} and holes[{other}] overlap")
        holes.append(hole)
    outline = Outline(outer, tuple(holes))
    if net_area(outline) <= 0:
        raise ValueError("the holes take the whole section: no area is left")
    return outline


def check_path(path: Any) -> str:
    """A profile's path under the library root, checked and with forward slashes.

    Args:
        path: ``<standard>/<type>/<size>.sldlfp``, e.g.
            ``Opus/rhs-welded/80x60x2.7.sldlfp``.

    Returns:
        str: The path with ``/`` between its three parts.

    Raises:
        ValueError: Not three parts, an absolute path or one leaving the
            root, no ``.sldlfp``, or a name Windows or SolidWorks refuses.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be <standard>/<type>/<size>.sldlfp")
    parts = path.replace("\\", "/").split("/")
    if path.startswith(("/", "\\")) or (len(parts[0]) >= 2 and parts[0][1] == ":"):
        raise ValueError(f"path {path!r} must be relative to the library root")
    if len(parts) != 3 or not all(parts):
        raise ValueError(
            f"path {path!r} must be <standard>/<type>/<size>.sldlfp: SolidWorks "
            "reads a library with one file per size as standard and type folders"
        )
    if not parts[2].lower().endswith(SUFFIX) or len(parts[2]) == len(SUFFIX):
        raise ValueError(f"path {path!r} must end in <size>.sldlfp")
    for index, part in enumerate(parts):
        stem = part[: -len(SUFFIX)] if index == 2 else part
        if part in (".", ".."):
            raise ValueError(f"path {path!r} must stay inside the library root")
        bad = sorted({ch for ch in part if ch in _REFUSED_CHARACTERS or ord(ch) < 32})
        if bad:
            raise ValueError(
                f"path {path!r}: {part!r} has {''.join(bad)!r}, which Windows or "
                "SolidWorks refuse in a file name"
            )
        if part != part.strip() or part.endswith(".") or stem.endswith((" ", ".")):
            raise ValueError(
                f"path {path!r}: {part!r} starts or ends with a space or a dot"
            )
        if stem.split(".")[0].lower() in _DEVICE_NAMES:
            raise ValueError(f"path {path!r}: {part!r} is a name Windows keeps")
    return "/".join(parts)


def path_key(path: str) -> str:
    """The key two spellings of one profile path share (Windows paths ignore case)."""
    return path.replace("\\", "/").casefold()


def digest(path: str) -> tuple[str, int]:
    """A file's SHA-256 (hex) and size in bytes."""
    sha = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
            size += len(block)
    return sha.hexdigest(), size


def exact_case(target: str) -> None:
    """Rename the file at ``target`` to its exact spelling when only the case differs.

    A file replaced on Windows may keep the old file's spelling
    (``80X60X2.7.SLDLFP``). Best effort: the file is in place either way, and
    Windows finds it under any case, so a refused rename changes nothing.
    """
    folder, name = os.path.split(target)
    try:
        for entry in os.listdir(folder):
            if entry != name and entry.casefold() == name.casefold():
                os.replace(os.path.join(folder, entry), target)
                return
    except OSError:
        return


# --- SolidWorks -----------------------------------------------------------

#: ``swUserPreferenceStringValue_e.swFileLocationsWeldmentProfiles``.
_WELDMENT_PROFILES = 29
#: ``swSketchCheckFeatureProfileUsage_e.swSketchCheckFeature_SWEEPSECTION``:
#: a structural member sweeps its profile along its line.
_SWEEP_SECTION = 15
#: ``swSketchCheckFeatureStatus_e``, in words, for the ones an outline can give.
_CHECK_STATUS = {
    -1: "an unknown error",
    1: "two entities cross",
    2: "an entity crosses itself",
    3: "a bad entity",
    4: "three entities meet at one point",
    5: "the sketch is empty",
    6: "an open contour",
    7: "too many contours",
    8: "an entity of no length",
    9: "several open contours",
    11: "open and closed contours mixed",
    12: "two contours cross",
    13: "contours apart from each other",
    14: "an open contour where a closed one is needed",
    16: "a contour inside a hole",
    17: "more than one contour",
    22: "an open or unclear contour",
}
#: ``swSketchSegments_e``: lines and arcs.
_LINE, _ARC = 0, 1
#: ``swCustomInfoType_e.swCustomInfoText`` and
#: ``swCustomPropertyAddOption_e.swCustomPropertyReplaceValue``.
_TEXT, _REPLACE = 30, 2
#: Seconds a dialog may wait while a profile is built before it is refused
#: (``DialogWatch`` with ``decline``) and the profile fails.
DIALOG_SECONDS = 10.0
#: The operation each profile is built in (``_handle_com_operation``). It makes
#: and closes a document, so the adapter pauses after it as after one.
OPERATION = "build_weldment_profile"
#: Error text of a SolidWorks that is gone (RPC unavailable, call failed,
#: disconnected) or of a connection broken in this process (the interface is
#: unknown, RPC_S_UNKNOWN_IF: every call failed so, seen live): every later
#: profile would fail the same way.
_LOST_MARKS = (
    "-2147023174",
    "-2147023170",
    "-2147417848",
    "-2147023179",
    "rpc server is unavailable",
    "has disconnected",
    "interface is unknown",
)


def _flagged(obj: Any, interface: str) -> Any:
    return _sw_type_info.flagged(obj, interface)


def _m(value: float) -> float:
    """Millimetres to metres."""
    return value / 1000.0


def profiles_root(adapter: Any, root: str | None) -> str:
    """The library folder: ``root``, or the first Weldment Profiles file location.

    A COM failure reading the setting is raised as it is, so a SolidWorks that
    is gone says so rather than seeming to have no folder set.

    Raises:
        LookupError: No ``root`` and no Weldment Profiles folder set.
    """
    if root:
        return os.path.abspath(root)
    setting = adapter.swApp.GetUserPreferenceStringValue(_WELDMENT_PROFILES)
    folders = [p.strip() for p in str(setting or "").split(";") if p.strip()]
    if not folders:
        raise LookupError(
            "No weldment profile folder: give root, or set Tools > Options > File "
            "Locations > Weldment Profiles in SolidWorks"
        )
    return os.path.abspath(folders[0])


def _open_documents(adapter: Any) -> list[tuple[str, str]]:
    """The title and file of each document open in SolidWorks, hidden ones too."""
    found = []
    for document in as_list(
        adapter._attempt(lambda: adapter.swApp.GetDocuments(), default=None)
    ):
        doc = _flagged(document, "IModelDoc2")
        title = adapter._attempt(lambda d=doc: d.GetTitle(), default="")
        path = adapter._attempt(lambda d=doc: d.GetPathName(), default="")
        found.append((str(title or ""), str(path or "")))
    return found


def _refuse_if_open(adapter: Any, name: str) -> None:
    """Refuse a profile whose file name is open in SolidWorks, from any folder.

    SolidWorks saves a new part under the name of an open document (seen
    live), and two documents of one name are one too many to close by name.

    Raises:
        ValueError: Naming the open document.
    """
    for _title, path in _open_documents(adapter):
        if path and os.path.basename(path).casefold() == name.casefold():
            raise ValueError(
                f"{path} is open in SolidWorks; close it (close_model) and build "
                "this profile again: SolidWorks keeps one document of a name open"
            )


def _new_part(adapter: Any) -> Any:
    """A new part from the default part template (``NewPart``, else ``NewDocument``).

    A COM failure is raised as it is, so a SolidWorks that is gone says so
    and the batch stops (:func:`lost_solidworks`).
    """
    app = adapter.swApp
    model = app.NewPart()
    if not model:
        template = adapter._resolve_template_path([8, 0, 1, 2, 3], ".prtdot")
        if template:
            model = app.NewDocument(template, 0, 0, 0)
    if not model:
        raise Exception(
            "SolidWorks made no new part: check Tools > Options > Default Templates"
        )
    adapter._attempt(lambda: _sw_type_info.flag_doc(model, 1), default=0)
    return model


def _draw(adapter: Any, model: Any, outline: Outline, define: bool) -> tuple[Any, str]:
    """The profile's sketch on the Front plane, closed again; its feature and definition.

    Lines go in as given; arcs through their ends and their midpoint
    (``Create3PointArc``), so each loop closes exactly. Then a point for
    each pierce point, and with ``define`` SolidWorks' Fully Define Sketch.

    Raises:
        ValueError: SolidWorks refused an entity.
    """
    front = main_planes(model)[2]
    model.ClearSelection2(True)
    if not front.Select2(False, 0):
        raise Exception("The Front plane could not be selected")
    manager = _flagged(model.SketchManager, "ISketchManager")
    solve_on(adapter, manager)
    manager.InsertSketch(True)
    try:
        sketch = _flagged(manager.ActiveSketch, "ISketch")
        manager.AddToDB = True
        try:
            for loop in outline.loops:
                for segment in loop:
                    if segment.kind == "line":
                        made = manager.CreateLine(
                            _m(segment.a[0]),
                            _m(segment.a[1]),
                            0.0,
                            _m(segment.b[0]),
                            _m(segment.b[1]),
                            0.0,
                        )
                    else:
                        mid = segment.mid()
                        made = manager.Create3PointArc(
                            _m(segment.a[0]),
                            _m(segment.a[1]),
                            0.0,
                            _m(segment.b[0]),
                            _m(segment.b[1]),
                            0.0,
                            _m(mid[0]),
                            _m(mid[1]),
                            0.0,
                        )
                    if made is None:
                        raise ValueError(
                            f"SolidWorks refused the {segment.kind} from "
                            f"{list(segment.a)} to {list(segment.b)}"
                        )
            for name, (x, y) in pierce_points(outline):
                if manager.CreatePoint(_m(x), _m(y), 0.0) is None:
                    raise ValueError(f"SolidWorks refused the {name} pierce point")
        finally:
            manager.AddToDB = False
        definition = sketch_definition(sketch)
        if define:
            try:
                definition = str(
                    define_sketch(adapter, model, manager, sketch).get("definition")
                )
            except Exception as error:  # the profile is sound without it
                definition = f"{sketch_definition(sketch)} (not defined: {error})"
    finally:
        manager.InsertSketch(True)
    return _flagged(model.FeatureByPositionReverse(0), "IFeature"), definition


def check_sketch(adapter: Any, sketch: Any, outline: Outline) -> None:
    """SolidWorks' own check that the sketch is the outline and sweeps as a profile.

    The lines and arcs are counted, and ``ISketch::CheckFeatureUse`` for a
    sweep section must find no fault and one closed contour per loop.

    Raises:
        ValueError: A count or the check is off; the message says how.
    """
    found = {"lines": 0, "arcs": 0}
    for segment in as_list(sketch.GetSketchSegments()):
        kind = int(_flagged(segment, "ISketchSegment").GetType())
        if kind == _LINE:
            found["lines"] += 1
        elif kind == _ARC:
            found["arcs"] += 1
    wanted = outline.counts()
    if found != wanted:
        raise ValueError(
            f"The sketch has {found['lines']} line(s) and {found['arcs']} arc(s), "
            f"the outline {wanted['lines']} and {wanted['arcs']}: SolidWorks "
            "merged or dropped some"
        )
    opened, closed = _byref_int(), _byref_int()
    status = int(sketch.CheckFeatureUse(_SWEEP_SECTION, opened, closed))
    open_count = int(getattr(opened, "value", 0) or 0)
    closed_count = int(getattr(closed, "value", 0) or 0)
    if status != 0 or open_count or closed_count != len(outline.loops):
        what = _CHECK_STATUS.get(status, f"status {status}")
        raise ValueError(
            f"SolidWorks' check of the sketch as a profile found "
            f"{what if status else 'no fault'}, {open_count} open and "
            f"{closed_count} closed contour(s) for {len(outline.loops)} loop(s)"
        )


def _set_properties(adapter: Any, model: Any, properties: dict[str, str]) -> None:
    """The file's custom properties (a cut-list item takes them over).

    Raises:
        ValueError: SolidWorks didn't store one.
    """
    manager = _flagged(
        model.Extension.CustomPropertyManager(""), "ICustomPropertyManager"
    )
    for name, value in properties.items():
        manager.Add3(name, _TEXT, value, _REPLACE)
        stored = adapter._attempt(lambda n=name: manager.Get(n), default=None)
        if str(stored) != value:
            raise ValueError(
                f"SolidWorks didn't store the property {name!r} (read back {stored!r})"
            )


def _save(model: Any, sketch: Any, path: str) -> None:
    """Save the part as a library feature part, the sketch its feature.

    Raises:
        Exception: SolidWorks reported an error or wrote no file.
    """
    model.ClearSelection2(True)
    sketch.Select2(False, 0)
    extension = _flagged(model.Extension, "IModelDocExtension")
    errors, warnings = _byref_int(), _byref_int()
    saved = extension.SaveAs2(
        path,
        SAVE_AS_CURRENT_VERSION,
        SAVE_AS_SILENT,
        _null_callout(),
        "",
        False,
        errors,
        warnings,
    )
    model.ClearSelection2(True)
    code = int(getattr(errors, "value", 0) or 0)
    if not saved or code or _written(path) is None:
        raise Exception(
            f"SolidWorks didn't save {path} (SaveAs2 returned {saved!r}, "
            f"swFileSaveError_e {code})"
        )


def _written(path: str) -> str | None:
    """The file SolidWorks wrote at ``path``, whatever the case of its name.

    SolidWorks writes a new file's extension in capitals (``.SLDLFP``).
    """
    folder, name = os.path.split(path)
    if not os.path.isdir(folder):
        return None
    for entry in os.listdir(folder):
        if entry.casefold() == name.casefold():
            return os.path.join(folder, entry)
    return None


def _ours(title: str, path: str, document: tuple[str, str]) -> bool:
    """Whether an open document is the profile's part: by its path once saved."""
    if path:
        return document[1].casefold() == path.casefold()
    return document == (title, "")


def _close(adapter: Any, model: Any) -> str | None:
    """Close the profile's part, never another document; what is left open, if anything.

    ``IModelDoc2::Close`` is not implemented (E_NOTIMPL, seen live on 2025),
    so ``CloseDoc`` closes it: by its full path once saved, which picked the
    right one of two documents of one name (seen live); an unsaved part by
    its title, unless a saved document shows the same title (Windows hiding
    known extensions shows ``80x60x2.7.SLDPRT`` as ``80x60x2.7``). Never
    raises: it runs on every path out of a build.
    """
    title = str(adapter._attempt(lambda: model.GetTitle(), default="") or "")
    path = str(adapter._attempt(lambda: model.GetPathName(), default="") or "")
    if path:
        adapter._attempt(lambda: adapter.swApp.CloseDoc(path))
    elif title and not any(t == title and p for t, p in _open_documents(adapter)):
        adapter._attempt(lambda: adapter.swApp.CloseDoc(title))
    if any(_ours(title, path, d) for d in _open_documents(adapter)):
        return path or title
    return None


def _in_solidworks(
    adapter: Any,
    staged: str,
    outline: Outline,
    properties: dict[str, str],
    define: bool,
) -> str:
    """One profile in SolidWorks: a new part, its sketch and properties saved at ``staged``.

    The part is closed on every path out, an interrupt too, and its closing
    checked. A dialog left open ``DIALOG_SECONDS`` is refused (Cancel, No,
    Abort, its only button, or closed) and fails the profile; the watch starts
    before the part is made.

    A failure is kept as its text, and the part's dispatches are let go
    before this returns or raises. A failure kept as the exception would hold
    them (its traceback's frames) in a reference cycle through this frame,
    released only by a garbage collection, perhaps after the adapter's
    ``CoUninitialize``: a dispatch released then broke the next connection
    in the process (every call ``RPC_S_UNKNOWN_IF``, 0x800706B5, seen live).

    Returns:
        str: The saved sketch's definition (``fully defined``, ``under defined``...).

    Raises:
        Exception: The profile couldn't be built, a dialog was refused, or the
            part stayed open; the message says which.
    """
    _refuse_if_open(adapter, os.path.basename(staged))
    pid = getattr(adapter, "_watch_pid", None)
    watch = (
        DialogWatch(pid, DIALOG_SECONDS, decline=True, name=OPERATION) if pid else None
    )
    model: Any = None
    sketch: Any = None
    failure: str | None = None
    left_open: str | None = None
    definition = ""
    with watch or nullcontext():
        try:
            model = _new_part(adapter)
            pause_after_document()
            sketch, definition = _draw(adapter, model, outline, define)
            check_sketch(
                adapter, _flagged(sketch.GetSpecificFeature2(), "ISketch"), outline
            )
            _set_properties(adapter, model, properties)
            _save(model, sketch, staged)
        except Exception as error:
            failure = str(error) or type(error).__name__
        finally:
            if model is not None:
                left_open = _close(adapter, model)
            model = sketch = None
    notes = [failure] if failure else []
    answered = [s for s in (watch.seen if watch else []) if s.answer]
    if answered:
        notes.append(refused_message(OPERATION, answered))
    if left_open:
        notes.append(f"SolidWorks didn't close {left_open}; close it (close_model)")
    if notes:
        raise Exception("; ".join(notes))
    return definition


def _promote(workdir: str, name: str, target: str) -> tuple[str, int]:
    """Move the saved profile into the library under its exact name; its SHA-256 and size.

    The file goes in as a ``.part`` copy beside the target, renamed over it,
    so a failure leaves the target as it was.

    Raises:
        Exception: SolidWorks wrote no file, or it couldn't be moved.
    """
    written = _written(os.path.join(workdir, name))
    if written is None:
        raise Exception(f"SolidWorks wrote no {name}")
    sha, size = digest(written)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    part = target + ".part"
    try:
        shutil.copyfile(written, part)
        os.replace(part, target)
    finally:
        if os.path.exists(part):
            os.remove(part)
    exact_case(target)
    return sha, size


def _build(
    adapter: Any, target: str, spec: dict[str, Any], define: bool
) -> dict[str, Any]:
    """Build one profile in a private folder, then move it into the library.

    Returns:
        dict[str, Any]: ``definition``, ``sha256`` and ``bytes``.

    Raises:
        Exception: Why the profile wasn't built; the library is unchanged.
    """
    outline = check_outline(spec["outline"])
    workdir = tempfile.mkdtemp(prefix="solidworks-mcp-profile-")
    try:
        name = os.path.basename(target)
        done = adapter._handle_com_operation(
            OPERATION,
            lambda: _in_solidworks(
                adapter,
                os.path.join(workdir, name),
                outline,
                dict(spec.get("properties") or {}),
                define,
            ),
        )
        if not done.is_success:
            raise Exception(strip_op_prefix(done.error, OPERATION))
        sha, size = _promote(workdir, name, target)
        return {"definition": done.data, "sha256": sha, "bytes": size}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def lost_solidworks(error: str | None) -> bool:
    """Whether a profile's error says SolidWorks itself is gone."""
    text = (error or "").lower()
    return any(mark in text for mark in _LOST_MARKS)


def _item(root: str, spec: dict[str, Any], error: str | None) -> dict[str, Any]:
    """A profile's result before it is built: an error until it is."""
    path = str(spec["path"])
    return {
        "path": path,
        "file": os.path.join(root, *path.split("/")),
        "status": "error",
        "error": error,
        "seconds": 0.0,
        "sha256": None,
        "bytes": None,
        "definition": None,
    }


def _one(
    adapter: Any, root: str, spec: dict[str, Any], overwrite: bool, define: bool
) -> dict[str, Any]:
    """Build or skip one profile; its result, a failure included."""
    started = time.perf_counter()
    item = _item(root, spec, None)
    target = str(item["file"])
    existed = os.path.isfile(target)
    try:
        if existed and not overwrite:
            item["status"] = "skipped"
            item["sha256"], item["bytes"] = digest(target)
        else:
            item.update(status="built", **_build(adapter, target, spec, define))
    except Exception as error:  # one profile's failure is its own; the rest go on
        item["status"] = "error"
        kept = "; the file there is left as it was" if existed else ""
        item["error"] = f"{str(error) or type(error).__name__}{kept}"
    item["seconds"] = round(time.perf_counter() - started, 2)
    return item


class SolidWorksWeldmentProfilesMixin:
    """Weldment profile files built from section outlines."""

    @staticmethod
    def _adapter(obj: Any) -> Any:
        """Return the runtime adapter object for dynamic attribute access."""
        return cast(Any, obj)

    async def build_weldment_profiles(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Build profile files, each its own operation; one failing stops none.

        Each profile is built in a private folder in one adapter operation,
        and copied into the library only once it was saved, closed and
        checked: a profile that fails leaves the library as it was. Between
        profiles the call gives way to the event loop, so a cancelled call
        stops there, never inside a profile. A SolidWorks that stops
        answering, or a connection that broke, stops the batch. Each
        profile's operation switches the user's options off and puts them back
        (``user_settings``).

        Args:
            params: ``profiles`` (each ``path`` under the root, ``outline``
                and ``properties``), ``root`` (default: the first Weldment
                Profiles folder), ``overwrite`` and ``define``.

        Returns:
            AdapterResult[dict[str, Any]]: ``root`` and ``items``, one per
            profile in order: ``path``, ``file``, ``status`` (``built``,
            ``skipped`` when the file exists and ``overwrite`` is off, or
            ``error``), ``error``, ``seconds``, ``sha256``, ``bytes`` and
            ``definition``.
        """
        adapter = self._adapter(self)
        if not adapter.is_connected():
            return AdapterResult(
                status=AdapterResultStatus.ERROR, error="Not connected to SolidWorks"
            )
        overwrite = bool(params.get("overwrite"))
        define = str(params.get("define") or "dimensions") == "dimensions"
        found = adapter._handle_com_operation(
            "build_weldment_profiles",
            lambda: profiles_root(adapter, params.get("root")),
        )
        if not found.is_success:
            return AdapterResult(
                status=AdapterResultStatus.ERROR,
                error=found.error,
                execution_time=found.execution_time,
            )
        root = str(found.data)
        started = time.perf_counter()
        before = adapter.currentModel
        items: list[dict[str, Any]] = []
        lost: str | None = None
        try:
            for spec in params.get("profiles") or []:
                if lost:
                    items.append(_item(root, spec, f"not built: {lost}"))
                    continue
                items.append(_one(adapter, root, spec, overwrite, define))
                if lost_solidworks(items[-1]["error"]):
                    lost = (
                        f"SolidWorks stopped answering at {items[-1]['path']}; "
                        "restart it (and the MCP server) and build again"
                    )
                # The event loop runs here, so a cancel stops between profiles.
                await asyncio.sleep(0)
        finally:
            adapter.currentModel = before
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={"root": root, "items": items},
            execution_time=round(time.perf_counter() - started, 3),
        )
