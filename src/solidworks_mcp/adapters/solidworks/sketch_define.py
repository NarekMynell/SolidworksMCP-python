"""Fully defined generated sketches: every point tied to the part's planes or origin.

A sketch drawn from coordinates (a weldment skeleton, a plate outline) comes out
under defined: SolidWorks marks it ``(-)`` and any line can be dragged away. A
CAD specialist expects geometry tied to the part's axes, with dimensions they
can edit. These helpers do that for sketches the server draws:

- 3D sketch: the ends of lines that meet are merged into one node, and every
  node gets its distance to the Right, Top and Front planes (its x, y and z),
  or a coincident relation with a plane it lies on.
- 2D sketch: every point gets a horizontal and a vertical dimension from the
  origin (or a horizontal or vertical relation with it when on its axis), and
  every circle its diameter.

Checked live on SolidWorks 2025 SP4 before it was written:

- Line ends meeting at one node stay separate points until merged
  (``sgMERGEPOINTS`` on the selected points).
- ``AddDimension2`` between a 3D sketch point and a plane failed for most
  points of a large sketch when its text was placed beyond the point; with the
  text halfway between the point and the plane, a 6 x 6 grid (49 nodes) took
  every dimension and came out fully defined.
- With ``ISketchManager.AutoSolve`` off and ``ISldWorks.CommandInProgress`` on
  while dimensioning, that grid took 20 s instead of 43 s.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .. import sw_type_info as _sw_type_info
from .com_utils import as_list, attempt, double_array
from .features import _null_callout

#: ``swConstrainedStatus_e`` names.
DEFINITION = {
    1: "unknown",
    2: "under defined",
    3: "fully defined",
    4: "over defined",
    5: "no solution",
    6: "invalid solution",
    7: "auto-solve off",
}
#: ``swConstrainedStatus_e.swFullyConstrained``.
FULLY_DEFINED = 3
#: A coordinate this close to 0 (metres) lies on the plane.
ON_PLANE_M = 1e-7
#: Ends this close (mm) are one node.
NODE_TOLERANCE_MM = 0.01
#: How far aside (metres) a dimension's text moves when its first place is refused.
_TEXT_STEP_M = 0.06
#: ``swUserPreferenceToggle_e`` switched off before dimensioning: the Modify
#: box on a new dimension (10), numeric input (372), dimension only when a
#: value is typed (520) and the prompt when a dimension over-defines (100).
_QUIET_TOGGLES = (10, 372, 520, 100)
#: ``swSketchSegments_e``: lines and circles (arcs).
_LINE, _ARC = 0, 1


def _flagged(obj: Any, interface: str) -> Any:
    """``obj`` with ``interface``'s methods flagged for late binding."""
    return _sw_type_info.flagged(obj, interface)


def _off_drawings(adapter: Any, display: Any) -> None:
    """Keep a dimension these helpers add off drawings (``MarkedForDrawing`` off).

    They pin generated geometry to the part's planes (a node's x, a plate
    corner's offset from the origin) and size nothing a shop cuts: a
    platform's assembly drawing imported 385 of them onto its views. Model
    dimensions are imported marked-for-drawing only, so these stay out of
    ``insert_model_dimensions`` and the drawing package.
    """
    attempt(
        adapter,
        lambda: setattr(
            _flagged(display, "IDisplayDimension"), "MarkedForDrawing", False
        ),
    )


def definition(sketch: Any) -> str:
    """How defined an ``ISketch`` is, in words.

    Args:
        sketch: An ``ISketch``.

    Returns:
        str: ``"fully defined"``, ``"under defined"`` and so on.
    """
    return DEFINITION.get(int(sketch.GetConstrainedStatus()), "unknown")


@contextmanager
def quiet_solving(adapter: Any, manager: Any) -> Iterator[None]:
    """Auto-solve off and a command in progress while relations go in.

    Dimensions added one by one each re-solve the sketch; with auto-solve off
    SolidWorks solves once when it comes back on. Auto-solve always comes back
    on and the command flag goes back to what it was.

    Args:
        adapter: The connected adapter (its ``swApp``).
        manager: The ``ISketchManager`` of the open sketch.

    Yields:
        None: While the settings are on.
    """
    app = adapter.swApp
    for toggle in _QUIET_TOGGLES:
        attempt(adapter, lambda t=toggle: app.SetUserPreferenceToggle(t, False))
    busy = attempt(adapter, lambda: bool(app.CommandInProgress), False)
    attempt(adapter, lambda: setattr(app, "CommandInProgress", True))
    attempt(adapter, lambda: setattr(manager, "AutoSolve", False))
    try:
        yield
    finally:
        # Back on whatever it was: a part left with auto-solve off made the
        # next sketch refuse dimensions (seen live after a plate).
        solve_on(adapter, manager)
        attempt(adapter, lambda: setattr(app, "CommandInProgress", busy))


def solve_on(adapter: Any, manager: Any) -> None:
    """Turn automatic solving on, SolidWorks' default.

    Args:
        adapter: The connected adapter.
        manager: The part's ``ISketchManager``.
    """
    attempt(adapter, lambda: setattr(manager, "AutoSolve", True))


def main_planes(model: Any) -> dict[int, Any]:
    """The part's three main planes by the axis each is normal to.

    Templates name them in the user's language, so they are found by place:
    the first three reference planes are Front (normal z), Top (normal y) and
    Right (normal x).

    Args:
        model: The part's ``IModelDoc2``.

    Returns:
        dict[int, Any]: ``{0: Right, 1: Top, 2: Front}`` plane features.

    Raises:
        LookupError: When the part has fewer than three reference planes.
    """
    planes: list[Any] = []
    feature = model.FirstFeature()
    while feature is not None and len(planes) < 3:
        feature = _flagged(feature, "IFeature")
        if feature.GetTypeName2() == "RefPlane":
            planes.append(feature)
        feature = feature.GetNextFeature()
    if len(planes) < 3:
        raise LookupError("The part has no Front, Top and Right planes")
    front, top, right = planes
    return {0: right, 1: top, 2: front}


def origin_point(model: Any) -> Any:
    """The part origin's sketch point, to dimension 2D sketches from.

    Args:
        model: The part's ``IModelDoc2``.

    Returns:
        Any: The origin's ``ISketchPoint``.

    Raises:
        LookupError: When the part has no origin.
    """
    feature = model.FirstFeature()
    while feature is not None:
        feature = _flagged(feature, "IFeature")
        if feature.GetTypeName2() == "OriginProfileFeature":
            sketch = _flagged(feature.GetSpecificFeature2(), "ISketch")
            points = as_list(sketch.GetSketchPoints2())
            if points:
                return _flagged(points[0], "ISketchPoint")
        feature = feature.GetNextFeature()
    raise LookupError("The part has no origin")


def origin_in_sketch(adapter: Any, sketch: Any) -> tuple[float, float]:
    """Where the part origin projects into a 2D sketch, sketch metres.

    Args:
        adapter: The connected adapter (its ``swApp`` gives the math utility).
        sketch: The open 2D ``ISketch``.

    Returns:
        tuple[float, float]: The origin's sketch x and y.
    """
    app = _flagged(adapter.swApp, "ISldWorks")
    utility = _flagged(app.GetMathUtility(), "IMathUtility")
    point = _flagged(utility.CreatePoint(double_array([0.0, 0.0, 0.0])), "IMathPoint")
    moved = _flagged(
        point.MultiplyTransform(sketch.ModelToSketchTransform), "IMathPoint"
    )
    x, y = (float(v) for v in as_list(moved.ArrayData)[:2])
    return x, y


def _xyz(point: Any) -> tuple[float, float, float]:
    """An ``ISketchPoint``'s coordinates, metres."""
    return float(point.X), float(point.Y), float(point.Z)


def _key(point: Any) -> tuple[int, int, int]:
    """A point's node: its coordinates rounded to the node tolerance."""
    return tuple(round(v * 1000.0 / NODE_TOLERANCE_MM) for v in _xyz(point))  # type: ignore[return-value]


def merge_nodes(model: Any, sketch: Any) -> int:
    """Merge line ends that meet into one point per node.

    Args:
        model: The part's ``IModelDoc2``, the sketch open in it.
        sketch: The open ``ISketch``.

    Returns:
        int: How many nodes had ends merged.
    """
    nodes: dict[tuple[int, int, int], list[Any]] = {}
    for segment in as_list(sketch.GetSketchSegments()):
        if int(_flagged(segment, "ISketchSegment").GetType()) != _LINE:
            continue
        line = _flagged(segment, "ISketchLine")
        for end in (line.GetStartPoint2(), line.GetEndPoint2()):
            point = _flagged(end, "ISketchPoint")
            nodes.setdefault(_key(point), []).append(point)
    merged = 0
    for points in nodes.values():
        if len(points) < 2:
            continue
        model.ClearSelection2(True)
        if all(p.Select4(i > 0, _null_callout()) for i, p in enumerate(points)):
            if model.SketchAddConstraints("sgMERGEPOINTS") is not False:
                merged += 1
    model.ClearSelection2(True)
    return merged


def tie_nodes_to_planes(
    adapter: Any, model: Any, manager: Any, sketch: Any
) -> dict[str, Any]:
    """Dimension every point of the open 3D sketch to the three main planes.

    Each nonzero coordinate gets its distance to its plane, the text halfway
    between them; each zero coordinate a coincident relation with the plane.
    When SolidWorks refuses one of a point's dimensions, the point's other
    dimensions come off again and it gets a Fix relation instead, so the
    sketch still comes out fully defined. (Seen live on 2025 SP4: once a 2D
    sketch in the part was dimensioned from the origin, every later 3D sketch
    point refused its distance to the Top plane.)

    Args:
        adapter: The connected adapter.
        model: The part's ``IModelDoc2``.
        manager: Its ``ISketchManager``.
        sketch: The open 3D ``ISketch``.

    Returns:
        dict[str, Any]: ``nodes``, ``dimensions``, ``relations``, ``fixed``
        (points fixed instead), ``fixed_points`` (the first of them, mm),
        ``definition`` and ``seconds``.
    """
    started = time.perf_counter()
    planes = main_planes(model)
    dimensions = relations = 0
    fixed: list[list[float]] = []
    points = [_flagged(p, "ISketchPoint") for p in as_list(sketch.GetSketchPoints2())]
    with quiet_solving(adapter, manager):
        for point in points:
            xyz = _xyz(point)
            made: list[Any] = []
            for axis, plane in planes.items():
                if abs(xyz[axis]) <= ON_PLANE_M:
                    continue
                display = _dimension(model, point, plane, xyz, axis)
                if display is None:
                    break
                _off_drawings(adapter, display)
                made.append(display)
            else:
                for axis, plane in planes.items():
                    if abs(xyz[axis]) <= ON_PLANE_M:
                        _relate(model, "sgCOINCIDENT", point, _adding(plane))
                        relations += 1
                dimensions += len(made)
                continue
            _delete(model, made)
            _relate(model, "sgFIXED", point)
            fixed.append([round(v * 1000.0, 3) for v in xyz])
        model.ClearSelection2(True)
    return {
        "nodes": len(points),
        "dimensions": dimensions,
        "relations": relations,
        "fixed": len(fixed),
        "fixed_points": fixed[:20],
        "definition": definition(sketch),
        "seconds": round(time.perf_counter() - started, 2),
    }


def _relate(
    model: Any, relation: str, point: Any, also: Callable[[], Any] | None = None
) -> None:
    """Add a sketch relation to ``point`` and, when given, what ``also`` selects.

    Args:
        model: The part's ``IModelDoc2``.
        relation: The ``SketchAddConstraints`` name, e.g. ``sgCOINCIDENT``.
        point: The ``ISketchPoint``.
        also: Adds the second entity to the selection, or None for a
            one-entity relation such as ``sgFIXED``.
    """
    model.ClearSelection2(True)
    point.Select4(False, _null_callout())
    if also is not None:
        also()
    model.SketchAddConstraints(relation)
    model.ClearSelection2(True)


def _adding(plane: Any) -> Callable[[], Any]:
    """A callable adding ``plane`` to the selection."""

    def add() -> Any:
        return plane.Select2(True, 0)

    return add


def _delete(model: Any, dimensions: list[Any]) -> None:
    """Delete display dimensions added a moment ago.

    Selected by the dimension's full name: selecting the annotation itself
    doesn't take in a 3D sketch (seen live).
    """
    for display in dimensions:
        dimension = _flagged(
            _flagged(display, "IDisplayDimension").GetDimension2(0), "IDimension"
        )
        model.ClearSelection2(True)
        if model.Extension.SelectByID2(
            str(dimension.FullName), "DIMENSION", 0, 0, 0, False, 0, _null_callout(), 0
        ):
            model.EditDelete()
    model.ClearSelection2(True)


def text_places(xyz: tuple[float, float, float], axis: int) -> list[list[float]]:
    """Where to try the text of a point-to-plane dimension, best first (metres).

    Halfway between the point and the plane first. SolidWorks refuses the
    dimension when its text lands on other geometry (seen live: inside a
    column the brace node sat above), so then a little to either side, then
    a quarter and three quarters of the way.

    Args:
        xyz: The point.
        axis: The axis the plane is square to (0 x, 1 y, 2 z).

    Returns:
        list[list[float]]: Text positions to try in turn.
    """
    half = list(xyz)
    half[axis] = xyz[axis] / 2.0
    places = [half]
    for other in (a for a in range(3) if a != axis):
        for step in (_TEXT_STEP_M, -_TEXT_STEP_M):
            moved = list(half)
            moved[other] += step
            places.append(moved)
    for share in (0.25, 0.75):
        part = list(xyz)
        part[axis] = xyz[axis] * share
        places.append(part)
    return places


def _dimension(
    model: Any,
    point: Any,
    plane: Any,
    xyz: tuple[float, float, float],
    axis: int,
) -> Any:
    """Dimension ``point`` to ``plane``, trying text places until one takes.

    Returns:
        Any: The display dimension, or None when every place was refused.
    """
    for place in text_places(xyz, axis):
        model.ClearSelection2(True)
        if not (point.Select4(False, _null_callout()) and plane.Select2(True, 0)):
            return None
        display = model.AddDimension2(*place)
        if display is not None:
            return display
    return None


def tie_points_to_origin(
    adapter: Any, model: Any, manager: Any, sketch: Any
) -> dict[str, Any]:
    """Dimension every point of the open 2D sketch from the origin, and every circle.

    Each point gets a horizontal and a vertical dimension from the part's
    origin (a vertical or horizontal relation with it when it sits on the
    origin's axis); each circle its diameter. Lines need nothing more once
    their ends are fixed.

    Args:
        adapter: The connected adapter.
        model: The part's ``IModelDoc2``.
        manager: Its ``ISketchManager``.
        sketch: The open 2D ``ISketch``.

    Returns:
        dict[str, Any]: ``points``, ``dimensions``, ``relations``, ``fixed``
        (points fixed because a dimension was refused), ``failed_count``
        (diameters refused), ``definition`` and ``seconds``.
    """
    started = time.perf_counter()
    origin = origin_point(model)
    # Where the part origin lands in this sketch: (0, 0) on a main or offset
    # plane, anywhere on a plane through some other point.
    ox, oy = origin_in_sketch(adapter, sketch)
    dimensions = relations = failed = fixed = 0
    circles = [
        segment
        for segment in as_list(sketch.GetSketchSegments())
        if int(_flagged(segment, "ISketchSegment").GetType()) == _ARC
    ]
    points = [_flagged(p, "ISketchPoint") for p in as_list(sketch.GetSketchPoints2())]
    seen = {_key(p) for p in points}
    for circle in circles:
        centre = _flagged(
            _flagged(circle, "ISketchArc").GetCenterPoint2(), "ISketchPoint"
        )
        if _key(centre) not in seen:
            seen.add(_key(centre))
            points.append(centre)
    with quiet_solving(adapter, manager):
        for point in points:
            x, y, _z = _xyz(point)
            offsets = (("h", x - ox), ("v", y - oy))
            made: list[Any] = []
            for along, value in offsets:
                if abs(value) <= ON_PLANE_M:
                    continue
                model.ClearSelection2(True)
                if not (
                    point.Select4(False, _null_callout())
                    and origin.Select4(True, _null_callout())
                ):
                    break
                if along == "h":
                    display = model.AddHorizontalDimension2(ox + value / 2.0, y, 0.0)
                else:
                    display = model.AddVerticalDimension2(x, oy + value / 2.0, 0.0)
                if display is None:
                    break
                _off_drawings(adapter, display)
                made.append(display)
            else:
                for along, value in offsets:
                    if abs(value) <= ON_PLANE_M:
                        # On the origin's vertical (or horizontal) line.
                        relation = (
                            "sgVERTICALPOINTS2D"
                            if along == "h"
                            else "sgHORIZONTALPOINTS2D"
                        )
                        _relate(
                            model,
                            relation,
                            point,
                            lambda: origin.Select4(True, _null_callout()),
                        )
                        relations += 1
                dimensions += len(made)
                continue
            _delete(model, made)
            _relate(model, "sgFIXED", point)
            fixed += 1
        for circle in circles:
            arc = _flagged(circle, "ISketchArc")
            model.ClearSelection2(True)
            if not _flagged(circle, "ISketchSegment").Select4(False, _null_callout()):
                failed += 1
                continue
            centre = _flagged(arc.GetCenterPoint2(), "ISketchPoint")
            radius = float(arc.GetRadius())
            diameter = model.AddDimension2(
                float(centre.X) + 1.5 * radius, float(centre.Y) + 1.5 * radius, 0.0
            )
            if diameter is None:
                failed += 1
            else:
                _off_drawings(adapter, diameter)
                dimensions += 1
        model.ClearSelection2(True)
    return {
        "points": len(points),
        "dimensions": dimensions,
        "relations": relations,
        "fixed": fixed,
        "failed_count": failed,
        "definition": definition(sketch),
        "seconds": round(time.perf_counter() - started, 2),
    }


#: ``ISketchManager.FullyDefineSketch`` relations, among those that already
#: hold: horizontal, vertical, coincident, concentric and tangent
#: (``swSketchFullyDefineRelationType_e``). Equal, parallel, perpendicular,
#: collinear and midpoint are left out: in a sketch drawn from coordinates
#: they are often chance, and the relation would tie sizes meant apart.
DEFINE_RELATIONS = 2 | 4 | 512 | 64 | 8
#: ``swAutodimScheme_e.swAutodimSchemeBaseline``, and the placements:
#: horizontal dimensions above, vertical ones to the left.
_BASELINE, _ABOVE, _LEFT = 1, 1, -1
#: ``swAutodimStatus_e``: success, and the states a sketch can't be sized in
#: (3D sketch, empty, over defined).
_AUTODIM_SUCCESS = 0
_AUTODIM_SKIPPED = {5: "3D sketch", 6: "empty sketch", 7: "over defined"}
#: How far (m) an anchoring dimension's text sits beside the point.
_ANCHOR_TEXT_M = 0.01
#: ``swDimensionDrivenState_e.swDimensionDriven``: a reference dimension
#: (``swDimensionDriving`` is 2).
_DRIVEN = 1


def _points_and_centres(sketch: Any) -> list[Any]:
    """The sketch's points and its arcs' and circles' centres."""
    points = [_flagged(p, "ISketchPoint") for p in as_list(sketch.GetSketchPoints2())]
    seen = {_key(p) for p in points}
    for segment in as_list(sketch.GetSketchSegments()):
        if int(_flagged(segment, "ISketchSegment").GetType()) != _ARC:
            continue
        centre = _flagged(
            _flagged(segment, "ISketchArc").GetCenterPoint2(), "ISketchPoint"
        )
        if _key(centre) not in seen:
            seen.add(_key(centre))
            points.append(centre)
    return points


def _anchor_to_origin(adapter: Any, model: Any, sketch: Any) -> str | None:
    """Tie the sketch's point nearest the part origin to it; how, or None without points.

    On the origin: a coincident relation. Otherwise one axis at a time: a
    horizontal or vertical relation when the point is on the origin's axis,
    else a dimension. An axis something already holds (the origin on a line,
    a distance the agent dimensioned) is left: a tie that over defines the
    sketch, or that SolidWorks makes a driven dimension, is taken back. The
    dimensions stay on drawings: they place the shape, as a designer's would.
    """
    points = _points_and_centres(sketch)
    if not points:
        return None
    origin = origin_point(model)
    ox, oy = origin_in_sketch(adapter, sketch)
    point = min(points, key=lambda p: (float(p.X) - ox) ** 2 + (float(p.Y) - oy) ** 2)
    x, y, _z = _xyz(point)
    dx, dy = x - ox, y - oy
    app = adapter.swApp
    for toggle in _QUIET_TOGGLES:  # no "make it driven?" box on a redundant one
        attempt(adapter, lambda t=toggle: app.SetUserPreferenceToggle(t, False))
    with_origin = lambda: origin.Select4(True, _null_callout())  # noqa: E731
    if abs(dx) <= ON_PLANE_M and abs(dy) <= ON_PLANE_M:
        _relate(model, "sgCOINCIDENT", point, with_origin)
        if definition(sketch) == "over defined":
            attempt(adapter, lambda: model.EditUndo2(1))
            return "held already"
        return "coincident"
    made = []
    for along, value in (("h", dx), ("v", dy)):
        if definition(sketch) == "fully defined":
            break
        if abs(value) <= ON_PLANE_M:
            # On the origin's vertical (or horizontal) line.
            _relate(
                model,
                "sgVERTICALPOINTS2D" if along == "h" else "sgHORIZONTALPOINTS2D",
                point,
                with_origin,
            )
            if definition(sketch) == "over defined":
                attempt(adapter, lambda: model.EditUndo2(1))
                made.append("held")
            else:
                made.append("relation")
            continue
        model.ClearSelection2(True)
        point.Select4(False, _null_callout())
        with_origin()
        display = (
            model.AddHorizontalDimension2(ox + value / 2.0, y + _ANCHOR_TEXT_M, 0.0)
            if along == "h"
            else model.AddVerticalDimension2(x + _ANCHOR_TEXT_M, oy + value / 2.0, 0.0)
        )
        model.ClearSelection2(True)
        if display is None:
            made.append("refused")
            continue
        driven = attempt(
            adapter,
            lambda d=display: int(
                _flagged(
                    _flagged(d, "IDisplayDimension").GetDimension2(0), "IDimension"
                ).DrivenState
            ),
            None,
        )
        if driven == _DRIVEN or definition(sketch) == "over defined":
            _delete(model, [display])
            made.append("held")
        else:
            made.append("dimension")
    return " and ".join(made) or None


def define_sketch(
    adapter: Any, model: Any, manager: Any, sketch: Any
) -> dict[str, Any]:
    """Fully define the open 2D sketch, the way a designer finishes one.

    ``ISketchManager.FullyDefineSketch`` (SolidWorks' Fully Define Sketch)
    adds the relations that already hold and the sizes, then the point
    nearest the part origin is tied to it. Checked live on SolidWorks 2025
    SP4: Fully Define Sketch takes no datum from outside the sketch (the
    origin given as datum, or selected with ``swAutodimMarkOriginDatum``,
    still left the sketch free to slide), so it sizes the sketch against
    itself; the one tie to the origin fixes the rest. A rectangle and a
    circle, a centred rectangle, a circle on the origin, an L outline and a
    lone inclined line each came out fully defined in 0.3-0.6 s. A sketch
    already fully defined is left as it is.

    Args:
        adapter: The connected adapter.
        model: The part's ``IModelDoc2``.
        manager: Its ``ISketchManager``.
        sketch: The open 2D ``ISketch``.

    Returns:
        dict[str, Any]: ``definition`` (``"fully defined"``, ``"under
        defined"``...), ``anchor`` (how the sketch was tied to the origin,
        when it was), ``skipped`` (why nothing was added) and ``seconds``.
    """
    started = time.perf_counter()
    result: dict[str, Any] = {}
    if definition(sketch) != "fully defined":
        null = _null_callout()
        model.ClearSelection2(True)
        status = int(
            manager.FullyDefineSketch(
                True,
                True,
                DEFINE_RELATIONS,
                True,
                _BASELINE,
                null,
                _BASELINE,
                null,
                _ABOVE,
                _LEFT,
            )
        )
        if status in _AUTODIM_SKIPPED:
            result["skipped"] = _AUTODIM_SKIPPED[status]
        elif definition(sketch) == "under defined":
            anchor = _anchor_to_origin(adapter, model, sketch)
            if anchor:
                result["anchor"] = anchor
        if status not in (_AUTODIM_SUCCESS, *_AUTODIM_SKIPPED):
            result["autodim_status"] = status
    result["definition"] = definition(sketch)
    result["seconds"] = round(time.perf_counter() - started, 2)
    return result
