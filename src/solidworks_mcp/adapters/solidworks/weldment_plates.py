"""Weldment plates in one call: base, splice, gusset and cap plates with their holes.

A plate is an outline (a rectangle or any polygon) and its holes, drawn in the
plate's own x/y on the face its thickness grows from, placed by an origin, a
normal and an in-plane x axis. Each plate becomes a sketch tied to the origin
(every corner and hole centre dimensioned, every hole's diameter), on a plane
tied to the main planes, extruded as its own body (not merged into the
members), named for the plate, and described in the cut list so the shop gets
"PL15 250x250" instead of "Cut-List-Item37".

Plates whose normal runs along x, y or z sit on a plane offset from Right, Top
or Front (plates on one level share it). Any other plate stands on a plane
square to a short line in a "plate axes" 3D sketch, at the line's start, the
line's ends dimensioned to the main planes like a skeleton's nodes.

``export_plate_dxfs`` writes each distinct plate's flat face as a DXF for the
cutting table (holes included), named by mark, thickness and quantity, and a
CSV list of them.
"""

from __future__ import annotations

import csv
import math
import os
import re
from dataclasses import dataclass
from typing import Any, cast

from ...exceptions import InputRefused
from .. import sw_type_info as _sw_type_info
from ..base import AdapterResult
from .com_utils import as_list
from .feature_tree import move_into_folder
from .features import _REF_PLANE_DISTANCE, _null_callout, _offset_plane_distance
from .io import _as_com
from .part_features import _to_sketch
from .sketch_define import (
    main_planes,
    merge_nodes,
    solve_on,
    tie_nodes_to_planes,
    tie_points_to_origin,
)
from .weldments import _cut_list_folders, _need_part, _property

Vector = tuple[float, float, float]

#: ``swRefPlaneReferenceConstraints_e``: square to a line, through a point.
_REF_PLANE_PERPENDICULAR = 2
_REF_PLANE_COINCIDENT = 4
#: ``swEndConditions_e``.
_END_BLIND, _END_MID_PLANE = 0, 6
#: ``swCustomInfoType_e.swCustomInfoText`` and
#: ``swCustomPropertyAddOption_e.swCustomPropertyReplaceValue``.
_TEXT, _REPLACE = 30, 2
#: Length of a plate's axis line in the "plate axes" sketch, mm.
_AXIS_MM = 100.0
#: Along a main axis when the normal is this close to it.
_ALIGNED = 1e-9
#: Holes keep at least this much steel to the plate edge, mm.
_EDGE_MIN_MM = 1.0
#: ``swExportToDWG_e.swExportToDWG_ExportSelectedFacesOrLoops``.
_EXPORT_FACES = 2
_AXES = "XYZ"


def _sub(a: Vector, b: Vector) -> Vector:
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def _dot(a: Vector, b: Vector) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vector, b: Vector) -> Vector:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _along(v: Vector, n: Vector) -> Vector:
    """``v`` less its part along the unit vector ``n``: its projection on the plate."""
    k = _dot(v, n)
    return v[0] - k * n[0], v[1] - k * n[1], v[2] - k * n[2]


def _unit(v: Vector, what: str) -> Vector:
    length = math.sqrt(_dot(v, v))
    if length < 1e-9:
        raise ValueError(f"{what} has no length")
    return v[0] / length, v[1] / length, v[2] / length


@dataclass(frozen=True)
class Frame:
    """Where a plate sits: its origin (mm) and unit x, y and normal axes."""

    origin: Vector
    u: Vector
    v: Vector
    n: Vector

    def at(self, x: float, y: float, z: float = 0.0) -> Vector:
        """The model point (mm) at plate coordinates ``x, y`` and height ``z``."""
        return (
            self.origin[0] + x * self.u[0] + y * self.v[0] + z * self.n[0],
            self.origin[1] + x * self.u[1] + y * self.v[1] + z * self.n[1],
            self.origin[2] + x * self.u[2] + y * self.v[2] + z * self.n[2],
        )


def plate_frame(
    origin: list[float], normal: list[float], x_axis: list[float] | None = None
) -> Frame:
    """A plate's frame from its origin, normal and (optionally) in-plane x axis.

    Without ``x_axis``, a level plate's x runs along model x, and any other
    plate's y runs up (its x then follows from y and the normal).

    Args:
        origin: ``[x, y, z]`` mm.
        normal: The thickness direction; any length.
        x_axis: The plate's x direction; projected onto the plate.

    Returns:
        Frame: The plate's frame.

    Raises:
        ValueError: A zero normal, or an x axis along the normal.
    """
    n = _unit((normal[0], normal[1], normal[2]), "The plate's normal")
    at: Vector = (float(origin[0]), float(origin[1]), float(origin[2]))
    if x_axis is not None:
        given = (float(x_axis[0]), float(x_axis[1]), float(x_axis[2]))
        u = _unit(_along(given, n), "x_axis across the plate")
        return Frame(at, u, _cross(n, u), n)
    if abs(n[1]) > 0.9:
        u = _unit(_along((1.0, 0.0, 0.0), n), "x")
        return Frame(at, u, _cross(n, u), n)
    v = _unit(_along((0.0, 1.0, 0.0), n), "y")
    return Frame(at, _cross(v, n), v, n)


def main_axis(n: Vector) -> tuple[int, int] | None:
    """The main axis (0 x, 1 y, 2 z) and sign a unit normal runs along, if any."""
    for axis in range(3):
        if abs(abs(n[axis]) - 1.0) <= _ALIGNED:
            return axis, 1 if n[axis] > 0 else -1
    return None


def outline_of(spec: dict[str, Any]) -> list[tuple[float, float]]:
    """A plate's outline in its own x/y, mm: ``outline``, or a centred rectangle."""
    if spec.get("outline"):
        return [(float(p[0]), float(p[1])) for p in spec["outline"]]
    w, h = float(spec["width"]) / 2.0, float(spec["height"]) / 2.0
    return [(-w, -h), (w, -h), (w, h), (-w, h)]


def _area(points: list[tuple[float, float]]) -> float:
    """The signed area of a polygon (shoelace), mm²."""
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1], strict=True):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _inside(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Whether ``point`` lies inside ``polygon`` (even-odd rule)."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _edge_distance(
    point: tuple[float, float], polygon: list[tuple[float, float]]
) -> float:
    """The shortest distance from ``point`` to the polygon's edges, mm."""
    best = math.inf
    px, py = point
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = (
            0.0
            if length2 == 0
            else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length2))
        )
        best = min(best, math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)))
    return best


def check_plate(spec: dict[str, Any]) -> list[tuple[float, float]]:
    """Check a plate's outline and holes; its outline.

    Raises:
        ValueError: An outline with no area or a repeated corner, or a hole
            outside the plate or cutting its edge.
    """
    points = outline_of(spec)
    label = spec.get("mark") or spec.get("name") or "plate"
    for a, b in zip(points, points[1:] + points[:1], strict=True):
        if math.dist(a, b) < 1e-6:
            raise ValueError(f"{label}: the outline repeats the corner {list(a)}")
    if abs(_area(points)) < 1.0:
        raise ValueError(f"{label}: the outline has no area")
    for hole in spec.get("holes") or []:
        radius = float(hole["diameter"]) / 2.0
        for position in hole["positions"]:
            centre = (float(position[0]), float(position[1]))
            if not _inside(centre, points):
                raise ValueError(
                    f"{label}: the Ø{hole['diameter']:g} hole at {list(centre)} is outside the plate"
                )
            if _edge_distance(centre, points) < radius + _EDGE_MIN_MM:
                raise ValueError(
                    f"{label}: the Ø{hole['diameter']:g} hole at {list(centre)} cuts the plate's edge"
                )
    return points


def description(spec: dict[str, Any], points: list[tuple[float, float]]) -> str:
    """The cut-list description: ``PL15 250x250`` (thickness, then the outline's size).

    The size is the smallest rectangle the outline fits in, turned as it
    must be: a sloped fascia strip 3 m long and 190 mm wide is "3032x191",
    not the 2830x1105 box around it, so the sheet it is cut from is right.
    Sizes are given to the whole millimetre (the DXF keeps the exact shape).
    """
    size = sorted(smallest_rectangle(points), reverse=True)
    return f"PL{float(spec['thickness']):g} {size[0]:.0f}x{size[1]:.0f}"


def smallest_rectangle(points: list[tuple[float, float]]) -> tuple[float, float]:
    """The sides of the least-area rectangle around ``points``, turned to fit (one side on an edge)."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    best = (max(xs) - min(xs), max(ys) - min(ys))
    for a, b in zip(points, points[1:] + points[:1], strict=True):
        length = math.dist(a, b)
        if length < 1e-9:
            continue
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        along = [p[0] * ux + p[1] * uy for p in points]
        across = [-p[0] * uy + p[1] * ux for p in points]
        sides = (max(along) - min(along), max(across) - min(across))
        if sides[0] * sides[1] < best[0] * best[1] - 1e-6:
            best = sides
    return best


def _label(spec: dict[str, Any]) -> str:
    """The plate's name in the tree: ``BP-1 Base plate``."""
    mark = str(spec.get("mark") or "").strip()
    name = str(spec["name"]).strip()
    return f"{mark} {name}" if mark else name


def _m(point_mm: Vector) -> list[float]:
    return [c / 1000.0 for c in point_mm]


def _flagged(obj: Any, interface: str) -> Any:
    return _sw_type_info.flagged(obj, interface)


class SolidWorksWeldmentPlatesMixin:
    """Plates for weldments, and their DXFs."""

    @staticmethod
    def _adapter(obj: Any) -> Any:
        """Return the runtime adapter object for dynamic attribute access."""
        return cast(Any, obj)

    async def create_plates(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Build plates as separate bodies, each tied to the origin, named and described.

        Args:
            params: ``plates`` (each ``name``, ``thickness``, ``origin``,
                ``normal``, ``outline`` or ``width`` and ``height``, and
                optionally ``mark``, ``x_axis``, ``holes`` and
                ``thickness_side``), ``folder`` and ``define``.

        Returns:
            AdapterResult[dict[str, Any]]: ``plates`` (each ``name``,
            ``feature``, ``sketch``, ``plane``, ``body``, ``description``,
            ``holes`` and ``definition``), ``planes_made``,
            ``folder`` and ``bodies_after``.
        """
        adapter = self._adapter(self)
        if wrong := _need_part(adapter, "create_plates"):
            return wrong
        specs = list(params["plates"])
        define = str(params.get("define") or "dimensions") == "dimensions"
        folder = params.get("folder")

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            outlines = [check_plate(spec) for spec in specs]
            frames = [
                plate_frame(spec["origin"], spec["normal"], spec.get("x_axis"))
                for spec in specs
            ]
            manager = _flagged(model.SketchManager, "ISketchManager")
            if adapter._attempt(lambda: manager.ActiveSketch) is not None:
                raise ValueError(
                    "A sketch is open; close it with exit_sketch before adding plates"
                )
            features = _flagged(model.FeatureManager, "IFeatureManager")
            part = _flagged(model, "IPartDoc")
            if not adapter._attempt(lambda: bool(part.IsWeldment()), default=False):
                # A part of plates alone is a weldment too: each plate then
                # gets its cut-list item, description and DXF (a laser-cut
                # panel part had no cut list, seen live).
                adapter._attempt(lambda: features.InsertWeldmentFeature())
            planes_before = adapter._attempt(lambda: plane_names(model), default=set())
            planes = main_planes(model)
            made_names: list[str] = []
            tilted = [i for i, f in enumerate(frames) if main_axis(f.n) is None]
            axis_lines: dict[int, tuple[Any, Any]] = {}
            if tilted:
                sketch_name, lines = _draw_axes(
                    adapter, model, manager, [frames[i] for i in tilted], define
                )
                made_names.append(sketch_name)
                axis_lines = dict(zip(tilted, lines, strict=True))
            shared: dict[tuple[int, float], Any] = {}
            results: list[dict[str, Any]] = []
            planes_made = 0
            for index, (spec, frame, points) in enumerate(
                zip(specs, frames, outlines, strict=True)
            ):
                label = _label(spec)
                plane, new_plane = _plate_plane(
                    adapter,
                    model,
                    features,
                    planes,
                    frame,
                    axis_lines.get(index),
                    label,
                    shared,
                )
                if new_plane:
                    planes_made += 1
                    made_names.append(str(plane.Name))
                before = {
                    str(_flagged(b, "IBody2").Name)
                    for b in as_list(part.GetBodies2(0, True))
                }
                sketch, state = _plate_sketch(
                    adapter, model, manager, plane, frame, spec, points, define, label
                )
                feature = _extrude(adapter, model, features, sketch, frame, spec, label)
                made_names.append(feature)
                body = _name_new_body(part, before, label)
                results.append(
                    {
                        "name": label,
                        "feature": feature,
                        "sketch": sketch,
                        "plane": str(plane.Name),
                        "body": body,
                        "description": description(spec, points),
                        "thickness_mm": float(spec["thickness"]),
                        "holes": sum(
                            len(h["positions"]) for h in spec.get("holes") or []
                        ),
                        "definition": state,
                    }
                )
            _describe(adapter, model, specs, results)
            stray = adapter._attempt(
                lambda: drop_stray_planes(adapter, model, planes_before), default=[]
            )
            placed = None
            if folder:
                placed = move_into_folder(adapter, model, str(folder), made_names)
            bodies = as_list(
                adapter._attempt(lambda: part.GetBodies2(0, True), default=None)
            )
            adapter._attempt(lambda: model.GraphicsRedraw2())
            data: dict[str, Any] = {
                "plates": results,
                "planes_made": planes_made,
                "folder": placed,
                "bodies_after": len(bodies),
            }
            if stray:
                data["stray_planes_removed"] = stray
            return data

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("create_plates", run),
        )

    async def export_plate_dxfs(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Write a DXF of each distinct plate (any flat body that isn't a member), and a CSV list.

        Args:
            params: ``output_dir`` and optionally ``prefix``.

        Returns:
            AdapterResult[dict[str, Any]]: ``files`` (each ``file``, ``mark``,
            ``description``, ``thickness_mm``, ``quantity``, ``area_mm2``) and
            ``csv``.
        """
        adapter = self._adapter(self)
        if wrong := _need_part(adapter, "export_plate_dxfs"):
            return wrong
        out_dir = str(params["output_dir"])
        prefix = str(params.get("prefix") or "")

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            model_path = str(
                adapter._attempt(lambda: model.GetPathName(), default="") or ""
            )
            if not model_path:
                raise InputRefused(
                    "The part has never been saved; save it with save_file, then export its plates."
                )
            os.makedirs(out_dir, exist_ok=True)
            part = _flagged(model, "IPartDoc")
            files: list[dict[str, Any]] = []
            skipped: list[str] = []
            for folder in _cut_list_folders(adapter, model, update=True):
                bodies = as_list(
                    adapter._attempt(
                        lambda f=folder: _flagged(
                            f.GetSpecificFeature2(), "IBodyFolder"
                        ).GetBodies()
                    )
                )
                if not bodies:
                    continue
                body = _flagged(bodies[0], "IBody2")
                if _is_member(adapter, body):
                    continue
                face = adapter._largest_flat_face(body)
                volume = _volume(adapter, body)
                if face is None or volume is None or face[0] <= 0:
                    skipped.append(str(folder.Name))
                    continue
                thickness = volume / face[0] * 1000.0
                properties = _flagged(
                    folder.CustomPropertyManager, "ICustomPropertyManager"
                )
                mark = _text(adapter, properties, "MARK")
                text = _text(adapter, properties, "Description") or str(folder.Name)
                quantity = len(bodies)
                stem = _slug(f"{prefix}{mark or text}_t{thickness:g}_x{quantity}")
                path = os.path.join(out_dir, f"{stem}.dxf")
                model.ClearSelection2(True)
                if not _as_com(adapter, face[1], "IEntity").Select4(
                    False, _null_callout()
                ):
                    skipped.append(str(folder.Name))
                    continue
                ok = part.ExportToDWG2(
                    path, model_path, _EXPORT_FACES, True, None, False, False, 0, None
                )
                model.ClearSelection2(True)
                if not ok or not os.path.exists(path):
                    skipped.append(str(folder.Name))
                    continue
                files.append(
                    {
                        "file": path,
                        "mark": mark,
                        "description": text,
                        "thickness_mm": round(thickness, 2),
                        "quantity": quantity,
                        "area_mm2": round(face[0] * 1e6, 1),
                    }
                )
            listing = os.path.join(out_dir, f"{prefix}plates.csv")
            with open(listing, "w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "file",
                        "mark",
                        "description",
                        "thickness_mm",
                        "quantity",
                        "area_mm2",
                    ],
                )
                writer.writeheader()
                for row in files:
                    writer.writerow({**row, "file": os.path.basename(row["file"])})
            return {"files": files, "csv": listing, "skipped": skipped}

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("export_plate_dxfs", run),
        )


def _draw_axes(
    adapter: Any,
    model: Any,
    manager: Any,
    frames: list[Frame],
    define: bool,
    name: str = "Plate axes",
) -> tuple[str, list[tuple[Any, Any]]]:
    """A 3D sketch of one short line per tilted plate, from its origin along its normal.

    Returns:
        tuple: The sketch's name (``name``, numbered when taken), and each
        line's segment and start point.
    """
    model.ClearSelection2(True)
    solve_on(adapter, manager)
    manager.AddToDB = True
    manager.Insert3DSketch(True)
    try:
        for frame in frames:
            end = frame.at(0.0, 0.0, _AXIS_MM)
            if manager.CreateLine(*_m(frame.origin), *_m(end)) is None:
                raise ValueError(
                    f"SolidWorks refused the plate axis at {list(frame.origin)} mm"
                )
        sketch = _flagged(manager.ActiveSketch, "ISketch")
        if define:
            merge_nodes(model, sketch)
            tie_nodes_to_planes(adapter, model, manager, sketch)
    finally:
        manager.Insert3DSketch(True)
        manager.AddToDB = False
    feature = _flagged(model.FeatureByPositionReverse(0), "IFeature")
    feature.Name = _free_name(model, name)
    sketch = _flagged(feature.GetSpecificFeature2(), "ISketch")
    lines = []
    for segment in as_list(sketch.GetSketchSegments()):
        line = _flagged(segment, "ISketchLine")
        lines.append(
            (
                _flagged(segment, "ISketchSegment"),
                _flagged(line.GetStartPoint2(), "ISketchPoint"),
            )
        )
    return str(feature.Name), lines


_GENERIC_PLANE = re.compile(r"^Plane\d+$")


def plane_names(model: Any) -> set[str]:
    """The names of the part's reference planes now."""
    names = set()
    feature = model.FirstFeature()
    while feature is not None:
        feature = _flagged(feature, "IFeature")
        if feature.GetTypeName2() == "RefPlane":
            names.add(str(feature.Name))
        feature = feature.GetNextFeature()
    return names


def drop_stray_planes(adapter: Any, model: Any, before: set[str]) -> list[str]:
    """Delete planes a call left unnamed and unused (``Plane1``...); their names.

    Two frame parts of one build came out with planes named ``Plane1``,
    ``Plane2`` just before their plates, which nothing referenced (seen
    live, not reproduced alone); a specialist's tree must not keep them.
    """
    dropped = []
    for name in sorted(plane_names(model) - before):
        if not _GENERIC_PLANE.match(name):
            continue
        feature = _flagged(model.FeatureByName(name), "IFeature")
        children = as_list(
            adapter._attempt(lambda f=feature: f.GetChildren(), default=None)
        )
        if children:
            continue
        model.ClearSelection2(True)
        if feature.Select2(False, 0) and model.EditDelete():
            dropped.append(name)
        model.ClearSelection2(True)
    return dropped


def _free_name(model: Any, wanted: str) -> str:
    """``wanted``, or ``wanted 2``, ``wanted 3``... when a feature has it."""
    name, count = wanted, 1
    while model.FeatureByName(name) is not None:
        count += 1
        name = f"{wanted} {count}"
    return name


def _plate_plane(
    adapter: Any,
    model: Any,
    features: Any,
    planes: dict[int, Any],
    frame: Frame,
    axis_line: tuple[Any, Any] | None,
    label: str,
    shared: dict[tuple[int, float], Any],
    prefix: str = "Plates",
) -> tuple[Any, bool]:
    """The plane a plate's sketch goes on, and whether it was made for it.

    A plane offset from a main plane is named ``<prefix> <axis><offset>``
    (``Plates Y+2745``) and shared by every sketch on it in one call.
    """
    along = main_axis(frame.n)
    if along is not None:
        axis = along[0]
        offset = round(frame.origin[axis], 6)
        if abs(offset) < 1e-6:
            return planes[axis], False
        key = (axis, offset)
        if key in shared:
            return shared[key], False
        distance, flip = _offset_plane_distance(offset, False)
        model.ClearSelection2(True)
        planes[axis].Select2(False, 0)
        made = features.InsertRefPlane(
            _REF_PLANE_DISTANCE | flip, distance, 0, 0.0, 0, 0.0
        )
        model.ClearSelection2(True)
        if made is None:
            raise Exception(
                f"SolidWorks refused the plane for {label} ({_AXES[axis]} {offset:g} mm)"
            )
        plane = _flagged(made, "IFeature")
        plane.Name = _free_name(model, f"{prefix} {_AXES[axis]}{offset:+g}")
        _hide(model, plane)
        shared[key] = plane
        return plane, True
    segment, start = cast(tuple[Any, Any], axis_line)
    selection = _flagged(model.SelectionManager, "ISelectionMgr")
    model.ClearSelection2(True)
    first = selection.CreateSelectData()
    first.Mark = 0
    second = selection.CreateSelectData()
    second.Mark = 1
    if not (segment.Select4(False, first) and start.Select4(True, second)):
        raise Exception(f"The axis line of {label} could not be selected")
    made = features.InsertRefPlane(
        _REF_PLANE_PERPENDICULAR, 0.0, _REF_PLANE_COINCIDENT, 0.0, 0, 0.0
    )
    model.ClearSelection2(True)
    if made is None:
        raise Exception(f"SolidWorks refused the plane square to the axis of {label}")
    plane = _flagged(made, "IFeature")
    plane.Name = _free_name(model, f"{label} plane")
    _hide(model, plane)
    return plane, True


def _hide(model: Any, plane: Any) -> None:
    """Hide a reference plane the plates were drawn on."""
    model.ClearSelection2(True)
    if plane.Select2(False, 0):
        model.BlankRefGeom()
    model.ClearSelection2(True)


def _plate_sketch(
    adapter: Any,
    model: Any,
    manager: Any,
    plane: Any,
    frame: Frame,
    spec: dict[str, Any],
    points: list[tuple[float, float]],
    define: bool,
    label: str,
) -> tuple[str, str]:
    """The plate's outline and holes as a sketch on ``plane``; its name and definition."""
    model.ClearSelection2(True)
    solve_on(adapter, manager)
    if not plane.Select2(False, 0):
        raise Exception(f"The plane for {label} could not be selected")
    manager.InsertSketch(True)
    state = "not checked"
    try:
        sketch = _flagged(manager.ActiveSketch, "ISketch")
        corners = [_to_sketch(adapter, sketch, _m(frame.at(x, y))) for x, y in points]
        manager.AddToDB = True
        try:
            for a, b in zip(corners, corners[1:] + corners[:1], strict=True):
                if manager.CreateLine(a[0], a[1], 0.0, b[0], b[1], 0.0) is None:
                    raise ValueError(
                        f"{label}: SolidWorks refused an edge of the outline"
                    )
            for hole in spec.get("holes") or []:
                radius = float(hole["diameter"]) / 2000.0
                for x, y in hole["positions"]:
                    centre = _to_sketch(
                        adapter, sketch, _m(frame.at(float(x), float(y)))
                    )
                    if (
                        manager.CreateCircleByRadius(centre[0], centre[1], 0.0, radius)
                        is None
                    ):
                        raise ValueError(
                            f"{label}: SolidWorks refused the hole at {[x, y]}"
                        )
        finally:
            manager.AddToDB = False
        merge_nodes(model, sketch)
        if define:
            state = tie_points_to_origin(adapter, model, manager, sketch)["definition"]
    finally:
        manager.InsertSketch(True)
    feature = _flagged(model.FeatureByPositionReverse(0), "IFeature")
    feature.Name = _free_name(model, f"{label} sketch")
    return str(feature.Name), state


def _extrude(
    adapter: Any,
    model: Any,
    features: Any,
    sketch: str,
    frame: Frame,
    spec: dict[str, Any],
    label: str,
) -> str:
    """Extrude the plate's sketch by its thickness as a new body; the feature's name."""
    sketch_feature = _flagged(model.FeatureByName(sketch), "IFeature")
    plane_sketch = _flagged(sketch_feature.GetSpecificFeature2(), "ISketch")
    above = _to_sketch(adapter, plane_sketch, _m(frame.at(0.0, 0.0, 1.0)))[2]
    side = str(spec.get("thickness_side") or "normal")
    along_sketch = above > 0
    reverse = not along_sketch if side == "normal" else along_sketch
    end = _END_MID_PLANE if side == "mid" else _END_BLIND
    model.ClearSelection2(True)
    sketch_feature.Select2(False, 0)
    made = features.FeatureExtrusion3(
        True,
        False,
        reverse and side != "mid",
        end,
        _END_BLIND,
        float(spec["thickness"]) / 1000.0,
        0.0,
        False,
        False,
        False,
        False,
        0.0,
        0.0,
        False,
        False,
        False,
        False,
        False,  # Merge: each plate its own body
        False,
        True,
        0,
        0.0,
        False,
    )
    model.ClearSelection2(True)
    if made is None:
        raise Exception(f"SolidWorks refused to extrude {label}")
    feature = _flagged(made, "IFeature")
    feature.Name = _free_name(model, label)
    return str(feature.Name)


def _name_new_body(part: Any, before: set[str], label: str) -> str | None:
    """Name the body the plate's extrusion made after the plate."""
    for raw in as_list(part.GetBodies2(0, True)):
        body = _flagged(raw, "IBody2")
        if str(body.Name) not in before:
            body.Name = label
            return str(body.Name)
    return None


def _describe(
    adapter: Any, model: Any, specs: list[dict[str, Any]], results: list[dict[str, Any]]
) -> None:
    """Give each plate's cut-list item its description, mark and thickness."""
    by_body = {
        r["body"]: (spec, r)
        for spec, r in zip(specs, results, strict=True)
        if r["body"]
    }
    for folder in _cut_list_folders(adapter, model, update=True):
        bodies = as_list(
            adapter._attempt(
                lambda f=folder: _flagged(
                    f.GetSpecificFeature2(), "IBodyFolder"
                ).GetBodies()
            )
        )
        names = [str(_flagged(b, "IBody2").Name) for b in bodies]
        hit = next((by_body[n] for n in names if n in by_body), None)
        if hit is None:
            continue
        spec, result = hit
        properties = _flagged(folder.CustomPropertyManager, "ICustomPropertyManager")
        values = {
            "Description": result["description"],
            "THICKNESS": f"{float(spec['thickness']):g}",
        }
        if spec.get("mark"):
            values["MARK"] = str(spec["mark"])
        for key, value in values.items():
            adapter._attempt(
                lambda k=key, v=value, m=properties: m.Add3(k, _TEXT, v, _REPLACE)
            )


def _is_member(adapter: Any, body: Any) -> bool:
    """Whether a structural member feature made ``body``."""
    for feature in as_list(adapter._attempt(lambda: body.GetFeatures(), default=None)):
        if str(_flagged(feature, "IFeature").GetTypeName2()) == "WeldMemberFeat":
            return True
    return False


def _volume(adapter: Any, body: Any) -> float | None:
    """A body's volume, m³."""
    values = as_list(
        adapter._attempt(lambda: body.GetMassProperties(1.0), default=None)
    )
    return float(values[3]) if len(values) > 3 else None


def _text(adapter: Any, properties: Any, name: str) -> str:
    """A custom property's resolved text, or ''."""
    return _property(adapter, properties, name).strip()


def _slug(text: str) -> str:
    """A file name from free text: letters, digits, dot, dash and underscore."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
