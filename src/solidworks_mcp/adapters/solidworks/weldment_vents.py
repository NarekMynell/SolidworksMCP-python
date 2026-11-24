"""Vent and drain holes in hollow members, for hot-dip galvanizing.

A closed hollow member bursts in the zinc bath (trapped air and moisture turn
to steam at about 450 °C) and holds zinc it can't drain. EN ISO 14713-2 wants
at least two holes in every enclosed space, near its ends and diagonally
opposite, so air leaves at the top and zinc drains at the bottom of how the
piece hangs. This finds each member's walls from its own body (no profile
orientation needed): the axis from two walls, each wall's extent along it, the
outer faces. Near the end that hangs higher, a vent goes through the wall that
faces up; near the lower end, a drain through the wall that faces down.

Each hole is a circle on the wall's face, dimensioned from the origin (fully
defined), cut blind to the member's axis: through the near wall only, never
reaching the opposite one. Rectangular and square sections; a round tube has
no flat wall and is reported as skipped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, cast

from .. import sw_type_info as _sw_type_info
from ..base import AdapterResult
from .com_utils import as_list
from .feature_tree import move_into_folder
from .features import _null_callout
from .io import _as_com
from .part_features import _to_sketch
from .sketch_define import solve_on, tie_points_to_origin
from .weldments import _bodies, _named_bodies, _need_part

Vector = tuple[float, float, float]

#: Faces this close to square to the axis (|cos|) are walls.
_SQUARE = 0.02
#: ``swEndConditions_e.swEndCondBlind``.
_BLIND = 0


def _dot(a: Vector, b: Vector) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vector, b: Vector) -> Vector:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _unit(v: Vector) -> Vector:
    length = math.sqrt(_dot(v, v))
    return (
        (v[0] / length, v[1] / length, v[2] / length)
        if length > 1e-12
        else (0.0, 0.0, 0.0)
    )


def _add(a: Vector, b: Vector, k: float = 1.0) -> Vector:
    return (a[0] + k * b[0], a[1] + k * b[1], a[2] + k * b[2])


@dataclass(frozen=True)
class Face:
    """A planar face: outward unit normal, a point on it, its area and its points (mm)."""

    normal: Vector
    point: Vector
    area: float
    points: tuple[Vector, ...]


@dataclass(frozen=True)
class Wall:
    """An outer wall: outward normal, distance from the axis, extent along it (mm)."""

    normal: Vector
    distance: float
    start: float
    end: float


@dataclass(frozen=True)
class Hole:
    """A hole to cut: on the wall face at ``point``, ``depth`` deep, vent or drain."""

    kind: str
    point: Vector
    normal: Vector
    depth: float


def axis_of(faces: list[Face]) -> Vector | None:
    """The member's axis: square to its two largest non-parallel flat faces."""
    ordered = sorted(faces, key=lambda f: f.area, reverse=True)
    if not ordered:
        return None
    first = ordered[0].normal
    for face in ordered[1:]:
        axis = _cross(first, face.normal)
        if math.sqrt(_dot(axis, axis)) > 0.5:
            return _unit(axis)
    return None


def walls_of(faces: list[Face], axis: Vector, centre: Vector) -> list[Wall]:
    """The outer walls: per outward direction, the flat face farthest from the axis."""
    best: dict[tuple[int, int, int], Wall] = {}
    for face in faces:
        if abs(_dot(face.normal, axis)) > _SQUARE:
            continue
        distance = _dot(
            face.normal,
            (
                face.point[0] - centre[0],
                face.point[1] - centre[1],
                face.point[2] - centre[2],
            ),
        )
        if distance <= 0:
            continue  # an inner wall: its normal points at the axis
        along = [
            _dot(axis, (p[0] - centre[0], p[1] - centre[1], p[2] - centre[2]))
            for p in face.points
        ]
        if not along:
            continue
        key = tuple(round(c * 1000) for c in face.normal)
        wall = Wall(face.normal, distance, min(along), max(along))
        if key not in best or wall.distance > best[key].distance:
            best[key] = wall  # type: ignore[index]
    return list(best.values())


def vent_holes(
    axis: Vector,
    centre: Vector,
    walls: list[Wall],
    hang: Vector,
    from_end: float,
) -> list[Hole]:
    """A vent near the higher end through the upward wall, a drain near the lower end through the downward one.

    Args:
        axis: The member's unit axis.
        centre: A point on the axis (the body's centre of mass), mm.
        walls: Its outer walls.
        hang: Which way is up while the piece hangs in the bath.
        from_end: Hole centre from the wall's end, mm.

    Returns:
        list[Hole]: Two holes, or none when the member has under two walls.
    """
    if len(walls) < 2:
        return []
    up = _unit(hang)
    toward_top = 1.0 if _dot(axis, up) >= 0 else -1.0
    facing = sorted(walls, key=lambda w: _dot(w.normal, up))
    if abs(_dot(facing[-1].normal, up)) < 0.1:
        # A member hanging plumb: no wall faces up; use two opposite walls.
        first = facing[0]
        opposite = min(walls, key=lambda w: _dot(w.normal, first.normal))
        top_wall, bottom_wall = first, opposite
    else:
        top_wall, bottom_wall = facing[-1], facing[0]

    def hole(kind: str, wall: Wall, high: bool) -> Hole:
        at = (
            wall.end - from_end if (high == (toward_top > 0)) else wall.start + from_end
        )
        on_axis = _add(centre, axis, at)
        return Hole(
            kind, _add(on_axis, wall.normal, wall.distance), wall.normal, wall.distance
        )

    return [hole("vent", top_wall, True), hole("drain", bottom_wall, False)]


def _flagged(obj: Any, interface: str) -> Any:
    return _sw_type_info.flagged(obj, interface)


def _xyz_mm(values: Any) -> Vector:
    data = [float(v) for v in as_list(values)[:3]]
    return (data[0] * 1000.0, data[1] * 1000.0, data[2] * 1000.0)


def _faces(adapter: Any, body: Any) -> list[Face]:
    """A body's flat faces, read through COM."""
    found: list[Face] = []
    for raw in as_list(adapter._attempt(lambda: body.GetFaces(), default=None)):
        face = _flagged(raw, "IFace2")
        surface = adapter._attempt(lambda f=face: _flagged(f.GetSurface(), "ISurface"))
        if surface is None or not adapter._attempt(
            lambda s=surface: bool(s.IsPlane()), default=False
        ):
            continue
        params = as_list(
            adapter._attempt(lambda s=surface: s.PlaneParams, default=None)
        )
        normal = as_list(adapter._attempt(lambda f=face: f.Normal, default=None))
        if len(params) < 6 or len(normal) < 3:
            continue
        points: list[Vector] = []
        for raw_edge in as_list(
            adapter._attempt(lambda f=face: f.GetEdges(), default=None)
        ):
            edge = _flagged(raw_edge, "IEdge")
            for vertex in (edge.GetStartVertex(), edge.GetEndVertex()):
                if vertex is not None:
                    points.append(_xyz_mm(_flagged(vertex, "IVertex").GetPoint()))
        found.append(
            Face(
                _unit((float(normal[0]), float(normal[1]), float(normal[2]))),
                (
                    float(params[3]) * 1000.0,
                    float(params[4]) * 1000.0,
                    float(params[5]) * 1000.0,
                ),
                float(adapter._attempt(lambda f=face: f.GetArea(), default=0.0)) * 1e6,
                tuple(points),
            )
        )
    return found


class SolidWorksWeldmentVentsMixin:
    """Vent and drain holes in hollow weldment members."""

    @staticmethod
    def _adapter(obj: Any) -> Any:
        """Return the runtime adapter object for dynamic attribute access."""
        return cast(Any, obj)

    async def create_vent_holes(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Cut a vent and a drain hole in each named hollow member.

        Args:
            params: ``members`` (feature or body names), ``diameter`` (mm),
                ``from_end_mm`` (30), ``hang`` (``[0, 1, 0]``), ``folder``
                ("Vent holes") and ``define`` ("dimensions").

        Returns:
            AdapterResult[dict[str, Any]]: ``holes`` (each ``member``,
            ``kind``, ``feature``, ``at_mm``), ``skipped`` (member and why)
            and ``folder``.
        """
        adapter = self._adapter(self)
        if wrong := _need_part(adapter, "create_vent_holes"):
            return wrong
        diameter = float(params["diameter"])
        from_end = float(params.get("from_end_mm") or 30.0)
        hang = tuple(float(v) for v in (params.get("hang") or [0.0, 1.0, 0.0]))
        define = str(params.get("define") or "dimensions") == "dimensions"
        folder = params.get("folder")

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            part = _flagged(model, "IPartDoc")
            manager = _flagged(model.SketchManager, "ISketchManager")
            if adapter._attempt(lambda: manager.ActiveSketch) is not None:
                raise ValueError("A sketch is open; close it with exit_sketch first")
            bodies = _named_bodies(adapter, model, part, list(params["members"]))
            plan: list[tuple[str, Hole]] = []
            skipped: list[dict[str, str]] = []
            for name, raw in bodies.items():
                body = _flagged(raw, "IBody2")
                faces = _faces(adapter, body)
                axis = axis_of(faces)
                mass = as_list(
                    adapter._attempt(
                        lambda b=body: b.GetMassProperties(1.0), default=None
                    )
                )
                if axis is None or len(mass) < 3:
                    skipped.append(
                        {
                            "member": name,
                            "why": "no flat walls (a round tube, or not a member)",
                        }
                    )
                    continue
                centre = (
                    float(mass[0]) * 1000.0,
                    float(mass[1]) * 1000.0,
                    float(mass[2]) * 1000.0,
                )
                walls = walls_of(faces, axis, centre)
                holes = vent_holes(axis, centre, walls, hang, from_end)  # type: ignore[arg-type]
                if not holes:
                    skipped.append(
                        {"member": name, "why": "fewer than two flat outer walls"}
                    )
                    continue
                too_small = [w for w in walls if 2 * w.distance < diameter + 4.0]
                if too_small:
                    skipped.append(
                        {
                            "member": name,
                            "why": f"a Ø{diameter:g} hole doesn't fit its walls",
                        }
                    )
                    continue
                plan += [(name, hole) for hole in holes]
            made: list[dict[str, Any]] = []
            features: list[str] = []
            # A cut renames the body it cuts (as a trim does): follow it.
            now_named = {name: name for name, _hole in plan}
            for index, (name, hole) in enumerate(plan, start=1):
                feature, renamed = _cut(
                    adapter,
                    model,
                    manager,
                    now_named[name],
                    hole,
                    diameter,
                    define,
                    f"{name} {hole.kind} Ø{diameter:g}",
                    index,
                )
                now_named[name] = renamed or now_named[name]
                features.append(feature)
                made.append(
                    {
                        "member": name,
                        "kind": hole.kind,
                        "feature": feature,
                        "at_mm": [round(c, 2) for c in hole.point],
                    }
                )
            placed = (
                move_into_folder(adapter, model, str(folder), features)
                if folder and features
                else None
            )
            adapter._attempt(lambda: model.GraphicsRedraw2())
            return {"holes": made, "skipped": skipped, "folder": placed}

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("create_vent_holes", run),
        )


def _select_wall(adapter: Any, model: Any, body: str, hole: Hole) -> bool:
    """Select the member's wall face the hole goes through, as an entity.

    Picking it by coordinates (``SelectByID2`` at the point) failed now and
    then on fresh members (seen live), so the face is found on the body,
    read again before every hole: the cut before may have changed it.
    """
    part = _flagged(model, "IPartDoc")
    current = _bodies(adapter, part).get(body)
    if current is None:
        return False
    for raw in as_list(adapter._attempt(lambda: current.GetFaces(), default=None)):
        face = _flagged(raw, "IFace2")
        normal = as_list(adapter._attempt(lambda f=face: f.Normal, default=None))
        surface = adapter._attempt(lambda f=face: _flagged(f.GetSurface(), "ISurface"))
        params = as_list(
            adapter._attempt(lambda s=surface: s.PlaneParams, default=None)
        )
        if len(normal) < 3 or len(params) < 6:
            continue
        n = _unit((float(normal[0]), float(normal[1]), float(normal[2])))
        if _dot(n, hole.normal) < 0.999:
            continue
        on_plane = (
            float(params[3]) * 1000.0,
            float(params[4]) * 1000.0,
            float(params[5]) * 1000.0,
        )
        offset = _dot(
            n,
            (
                hole.point[0] - on_plane[0],
                hole.point[1] - on_plane[1],
                hole.point[2] - on_plane[2],
            ),
        )
        if abs(offset) > 0.05:
            continue
        model.ClearSelection2(True)
        if _as_com(adapter, face, "IEntity").Select4(False, _null_callout()):
            return True
    return False


def _cut(
    adapter: Any,
    model: Any,
    manager: Any,
    body: str,
    hole: Hole,
    diameter: float,
    define: bool,
    label: str,
    index: int,
) -> tuple[str, str | None]:
    """One hole: a circle sketched on the wall at ``hole.point``, cut blind to the axis.

    Returns:
        tuple: The cut's name, and the name the cut body now has.
    """
    model.ClearSelection2(True)
    solve_on(adapter, manager)
    x, y, z = (c / 1000.0 for c in hole.point)
    if not _select_wall(adapter, model, body, hole):
        raise LookupError(
            f"No wall face at {[round(c, 1) for c in hole.point]} mm for {label}"
        )
    manager.InsertSketch(True)
    try:
        sketch = _flagged(manager.ActiveSketch, "ISketch")
        centre = _to_sketch(adapter, sketch, [x, y, z])
        manager.AddToDB = True
        try:
            if (
                manager.CreateCircleByRadius(
                    centre[0], centre[1], 0.0, diameter / 2000.0
                )
                is None
            ):
                raise ValueError(f"SolidWorks refused the circle of {label}")
        finally:
            manager.AddToDB = False
        if define:
            tie_points_to_origin(adapter, model, manager, sketch)
    finally:
        manager.InsertSketch(True)
    sketch_feature = _flagged(model.FeatureByPositionReverse(0), "IFeature")
    model.ClearSelection2(True)
    sketch_feature.Select2(False, 0)
    features = _flagged(model.FeatureManager, "IFeatureManager")
    cut = features.FeatureCut4(
        True,
        False,
        False,
        _BLIND,
        _BLIND,
        hole.depth / 1000.0,
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
        False,
        False,
        True,
        False,
        False,
        False,
        0,
        0.0,
        False,
        False,
    )
    model.ClearSelection2(True)
    if cut is None:
        raise Exception(f"SolidWorks refused to cut {label}")
    feature = _flagged(cut, "IFeature")
    name = f"{label} ({index})"
    feature.Name = name
    return str(feature.Name), _body_of(adapter, feature)


def _body_of(adapter: Any, feature: Any) -> str | None:
    """The name of the body a feature cut, read from one of its faces."""
    for raw in as_list(adapter._attempt(lambda: feature.GetFaces(), default=None)):
        face = _flagged(raw, "IFace2")
        body = adapter._attempt(lambda f=face: _flagged(f.GetBody(), "IBody2"))
        if body is not None:
            return str(body.Name)
    return None
