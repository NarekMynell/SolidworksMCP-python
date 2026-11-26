"""Repeat what is built instead of drawing it again: body patterns in a weldment.

One frame, baluster, purlin or plate is built, trimmed and capped once, then
copied: along a direction (one or two), around an axis, or mirrored. The
copies are bodies of one pattern feature, so a CAD specialist changes the
pitch or the count in one place and the cut list counts them with the seed.

Checked live on SolidWorks 2025 SP4 before it was written:

- ``FeatureLinearPattern5`` and ``FeatureCircularPattern5`` pattern bodies
  selected under mark 256, the direction (or axis) under mark 1 and a second
  direction under mark 2. Through ``CreateDefinition`` data with
  ``BodyPattern`` and ``PatternBodyArray`` the same patterns came back
  ``None``.
- ``InsertMirrorFeature(True, False, False, False)`` mirrors bodies (mark 256)
  about a plane (mark 2).
- The copies keep the members' cut-list properties: a mitred frame of five
  RHS bodies patterned 2 x 2 gave five items of quantity 4, each with its
  length and end angles; a mirrored rafter joined its seed's item.

Which way SolidWorks runs a pattern along an axis is its choice, so the first
copy is measured against the seed and the pattern made again the other way
when it went backwards.
"""

from __future__ import annotations

import math
from typing import Any, cast

from ...exceptions import InputRefused
from .. import sw_type_info as _sw_type_info
from ..base import AdapterResult
from .com_utils import as_list, long_array
from .feature_tree import last_folder_of, move_into_folder
from .features import _null_callout
from .sketch_define import main_planes, merge_nodes, solve_on, tie_nodes_to_planes
from .weldment_plates import (
    _draw_axes,
    _free_name,
    _plate_plane,
    main_axis,
    plate_frame,
)
from .weldments import _bodies, _mass, _named_bodies, _need_part

#: Selection marks the pattern and mirror calls read.
_MARK_DIRECTION_1, _MARK_DIRECTION_2, _MARK_BODIES, _MARK_PLANE = 1, 2, 256, 2
#: A copy this close (mm) to where it belongs is there.
_PLACED_MM = 1.0
#: Length of a drawn direction line, mm.
_LINE_MM = 100.0
#: Main plane names agents use, by the axis the plane is square to.
_PLANE_NAMES = {
    "right": 0,
    "yz": 0,
    "top": 1,
    "xz": 1,
    "front": 2,
    "xy": 2,
}
_AXES = "XYZ"


def _flagged(obj: Any, interface: str) -> Any:
    return _sw_type_info.flagged(obj, interface)


def _unit(v: list[float] | tuple[float, ...], what: str) -> tuple[float, float, float]:
    length = math.sqrt(sum(c * c for c in v))
    if length < 1e-9:
        raise InputRefused(f"{what} has no length")
    return (v[0] / length, v[1] / length, v[2] / length)


def _select(entity: Any, model: Any, append: bool, mark: int) -> bool:
    """Select a feature, body or sketch segment (``(kind, object)``) under a mark."""
    kind, target = entity
    if kind == "feature":
        return bool(_flagged(target, "IFeature").Select2(append, mark))
    selection = _flagged(model.SelectionManager, "ISelectionMgr")
    data = _flagged(selection.CreateSelectData(), "ISelectData")
    data.Mark = mark
    if kind == "body":
        return bool(_flagged(target, "IBody2").Select2(append, data))
    return bool(_flagged(target, "ISketchSegment").Select4(append, data))


def main_axis_feature(adapter: Any, model: Any, axis: int) -> Any:
    """The reference axis along model x, y or z through the origin, made once.

    Named ``X axis`` (``Y axis``, ``Z axis``): the line where two main planes
    meet, so it is fully defined and a specialist recognises it.
    """
    name = f"{_AXES[axis]} axis"
    found = model.FeatureByName(name)
    if found is not None and _flagged(found, "IFeature").GetTypeName2() == "RefAxis":
        return _flagged(found, "IFeature")
    planes = main_planes(model)
    a, b = [planes[i] for i in range(3) if i != axis]
    model.ClearSelection2(True)
    a.Select2(False, 0)
    b.Select2(True, 0)
    made = adapter._attempt(lambda: model.InsertAxis2(True), default=False)
    model.ClearSelection2(True)
    feature = _flagged(model.FeatureByPositionReverse(0), "IFeature")
    if not made or feature.GetTypeName2() != "RefAxis":
        raise Exception(f"SolidWorks refused the {name} where two main planes meet")
    feature.Name = _free_name(model, name)
    model.ClearSelection2(True)
    if feature.Select2(False, 0):
        model.BlankRefGeom()
    model.ClearSelection2(True)
    return feature


def _draw_line(
    adapter: Any, model: Any, start: tuple[float, ...], d: tuple[float, ...], name: str
) -> tuple[Any, str]:
    """A fully defined 3D sketch of one line from ``start`` (mm) along ``d``."""
    manager = _flagged(model.SketchManager, "ISketchManager")
    model.ClearSelection2(True)
    solve_on(adapter, manager)
    manager.AddToDB = True
    manager.Insert3DSketch(True)
    try:
        end = [start[k] + _LINE_MM * d[k] for k in range(3)]
        if (
            manager.CreateLine(*(c / 1000.0 for c in start), *(c / 1000.0 for c in end))
            is None
        ):
            raise Exception(f"SolidWorks refused the line for {name}")
        sketch = _flagged(manager.ActiveSketch, "ISketch")
        merge_nodes(model, sketch)
        tie_nodes_to_planes(adapter, model, manager, sketch)
    finally:
        manager.Insert3DSketch(True)
        manager.AddToDB = False
    feature = _flagged(model.FeatureByPositionReverse(0), "IFeature")
    feature.Name = _free_name(model, name)
    sketch = _flagged(feature.GetSpecificFeature2(), "ISketch")
    segment = _flagged(as_list(sketch.GetSketchSegments())[0], "ISketchSegment")
    return segment, str(feature.Name)


def direction_entity(
    adapter: Any,
    model: Any,
    start: tuple[float, ...],
    d: tuple[float, float, float],
    name: str,
    made: list[str],
) -> Any:
    """What a pattern runs along: a main axis, or a drawn line (named in ``made``)."""
    along = main_axis(d)
    on_axis = along is not None and all(
        abs(start[k]) < 1e-6 for k in range(3) if k != along[0]
    )
    if along is not None and on_axis:
        axis = main_axis_feature(adapter, model, along[0])
        made.append(str(axis.Name))
        return ("feature", axis)
    segment, sketch = _draw_line(adapter, model, start, d, name)
    made.append(sketch)
    return ("segment", segment)


def _centre(adapter: Any, body: Any) -> tuple[float, float, float] | None:
    mass = _mass(adapter, body)
    return None if mass is None else tuple(c * 1000.0 for c in mass[0])  # type: ignore[return-value]


def _copy_at(
    adapter: Any, part: Any, before: set[str], target: tuple[float, ...]
) -> bool:
    """Whether a body made by the pattern has its centre at ``target`` (mm)."""
    for name, body in _bodies(adapter, part).items():
        if name in before:
            continue
        centre = _centre(adapter, body)
        if centre is not None and math.dist(centre, target) <= _PLACED_MM:
            return True
    return False


def _rotated(
    point: tuple[float, ...],
    origin: tuple[float, ...],
    axis: tuple[float, float, float],
    angle: float,
) -> tuple[float, float, float]:
    """``point`` turned by ``angle`` radians about the line through ``origin``."""
    v = [point[k] - origin[k] for k in range(3)]
    c, s = math.cos(angle), math.sin(angle)
    k = (
        axis[1] * v[2] - axis[2] * v[1],
        axis[2] * v[0] - axis[0] * v[2],
        axis[0] * v[1] - axis[1] * v[0],
    )
    d = (axis[0] * v[0] + axis[1] * v[1] + axis[2] * v[2]) * (1.0 - c)
    return (
        origin[0] + v[0] * c + k[0] * s + axis[0] * d,
        origin[1] + v[1] * c + k[1] * s + axis[1] * d,
        origin[2] + v[2] * c + k[2] * s + axis[2] * d,
    )


class SolidWorksWeldmentPatternsMixin:
    """Linear, circular and mirror patterns of weldment bodies."""

    @staticmethod
    def _adapter(obj: Any) -> Any:
        """Return the runtime adapter object for dynamic attribute access."""
        return cast(Any, obj)

    async def pattern_members(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Copy members, plates or caps along a direction, around an axis, or mirrored.

        Args:
            params: ``members`` (member feature, body or plate names) and one
                of ``along`` (``direction``, ``spacing``, ``count``,
                optionally ``along2`` for a grid), ``around`` (``point``,
                ``axis``, ``count``, ``angle``) or ``mirror`` (``plane`` or
                ``point`` and ``normal``); ``skip``, ``name`` and ``folder``.

        Returns:
            AdapterResult[dict[str, Any]]: ``feature``, ``kind``,
            ``instances``, ``seed_bodies``, ``bodies_added``,
            ``bodies_after``, ``references`` and ``folder``.
        """
        adapter = self._adapter(self)
        if wrong := _need_part(adapter, "pattern_members"):
            return wrong

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            part = _flagged(model, "IPartDoc")
            manager = _flagged(model.SketchManager, "ISketchManager")
            if adapter._attempt(lambda: manager.ActiveSketch) is not None:
                raise ValueError("A sketch is open; close it with exit_sketch first")
            names = [str(n) for n in params["members"]]
            seeds = _named_bodies(adapter, model, part, names)
            before = set(_bodies(adapter, part))
            label = str(params.get("name") or f"{names[0]} pattern")
            made: list[str] = []

            def fresh() -> dict[str, Any]:
                # Found again by name: a pattern made and deleted (it ran the
                # wrong way) leaves the body pointers stale (seen live).
                return _named_bodies(adapter, model, part, names)

            if params.get("along"):
                feature, kind, instances = _linear(
                    adapter, model, part, fresh, before, params, label, made
                )
            elif params.get("around"):
                feature, kind, instances = _circular(
                    adapter, model, part, fresh, before, params["around"], label, made
                )
            else:
                feature, kind, instances = _mirror(
                    adapter, model, fresh, params["mirror"], label, made
                )
            feature.Name = _free_name(model, label)
            after = _bodies(adapter, part)
            added = len(after) - len(before)
            folder = params.get("folder") or last_folder_of(model, names)
            placed = None
            if folder:
                placed = move_into_folder(
                    adapter, model, str(folder), [*made, str(feature.Name)]
                )
            adapter._attempt(lambda: model.GraphicsRedraw2())
            data: dict[str, Any] = {
                "feature": str(feature.Name),
                "kind": kind,
                "instances": instances,
                "seed_bodies": len(seeds),
                "bodies_added": added,
                "bodies_after": len(after),
                "references": made,
                "folder": placed,
            }
            expected = len(seeds) * (instances - 1)
            if added != expected:
                data["warning"] = (
                    f"{added} body(ies) added where {expected} were expected: an "
                    "instance may have merged with another body or been skipped"
                )
            return data

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("pattern_members", run),
        )


def _select_seeds(model: Any, seeds: dict[str, Any], append: bool) -> None:
    for index, body in enumerate(seeds.values()):
        if not _select(("body", body), model, append or index > 0, _MARK_BODIES):
            raise Exception(f"SolidWorks wouldn't select the body {list(seeds)[index]}")


def _linear(
    adapter: Any,
    model: Any,
    part: Any,
    fresh: Any,
    before: set[str],
    params: dict[str, Any],
    label: str,
    made: list[str],
) -> tuple[Any, str, int]:
    """A linear pattern in one or two directions, run the way asked."""
    steps = [params["along"]] + ([params["along2"]] if params.get("along2") else [])
    directions = [_unit(s["direction"], "along.direction") for s in steps]
    refs = [
        direction_entity(
            adapter, model, (0.0, 0.0, 0.0), d, f"{label} direction {i + 1}", made
        )
        for i, d in enumerate(directions)
    ]
    centre = _centre(adapter, next(iter(fresh().values())))
    guess = [_reversed(ref, d) for ref, d in zip(refs, directions, strict=True)]
    features = _flagged(model.FeatureManager, "IFeatureManager")
    feature = None
    for attempt in range(2 ** len(steps)):
        # The axis's own way first (no retry), then the other ways round.
        flips = [guess[i] != bool(attempt >> i & 1) for i in range(len(steps))]
        model.ClearSelection2(True)
        for i, ref in enumerate(refs):
            if not _select(
                ref, model, i > 0, (_MARK_DIRECTION_1, _MARK_DIRECTION_2)[i]
            ):
                raise Exception("SolidWorks wouldn't select the pattern direction")
        _select_seeds(model, fresh(), True)
        two = len(steps) == 2
        made_feature = adapter._attempt(
            lambda f=flips, t=two: features.FeatureLinearPattern5(
                int(steps[0]["count"]),
                float(steps[0]["spacing"]) / 1000.0,
                int(steps[1]["count"]) if t else 1,
                float(steps[1]["spacing"]) / 1000.0 if t else 0.0,
                f[0],
                f[1] if t else False,
                "",
                "",
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
                0.0,
                0.0,
                False,
                False,
            ),
            default=None,
        )
        model.ClearSelection2(True)
        if made_feature is None:
            raise Exception(
                "SolidWorks refused the linear pattern: check the members exist and "
                "the copies don't land on top of each other"
            )
        feature = _flagged(made_feature, "IFeature")
        if centre is None:
            break
        right_way = all(
            _copy_at(
                adapter,
                part,
                before,
                tuple(centre[k] + float(s["spacing"]) * d[k] for k in range(3)),
            )
            for s, d in zip(steps, directions, strict=True)
        )
        if right_way:
            break
        _delete(model, feature)
        feature = None
    if feature is None:
        raise Exception("No way round put the copies where asked; check the direction")
    if params.get("skip"):
        try:
            _skip(adapter, model, feature, params["skip"], len(steps))
        except Exception:
            _delete(model, feature)  # no half-made pattern left behind
            raise
    count = int(steps[0]["count"]) * (int(steps[1]["count"]) if len(steps) == 2 else 1)
    return feature, "linear", count - len(params.get("skip") or [])


def _circular(
    adapter: Any,
    model: Any,
    part: Any,
    fresh: Any,
    before: set[str],
    around: dict[str, Any],
    label: str,
    made: list[str],
) -> tuple[Any, str, int]:
    """A circular pattern about an axis, the copies turning by the right hand."""
    axis = _unit(around["axis"], "around.axis")
    point = tuple(float(c) for c in around.get("point") or (0.0, 0.0, 0.0))
    count = int(around["count"])
    total = float(around.get("angle") or 360.0)
    step = total / count if abs(total - 360.0) < 1e-9 else total / (count - 1)
    ref = direction_entity(adapter, model, point, axis, f"{label} axis", made)
    centre = _centre(adapter, next(iter(fresh().values())))
    features = _flagged(model.FeatureManager, "IFeatureManager")
    feature = None
    for flip in (False, True):
        model.ClearSelection2(True)
        if not _select(ref, model, False, _MARK_DIRECTION_1):
            raise Exception("SolidWorks wouldn't select the pattern axis")
        _select_seeds(model, fresh(), True)
        made_feature = adapter._attempt(
            lambda f=flip: features.FeatureCircularPattern5(
                count,
                math.radians(total),
                f,
                "",
                False,
                True,
                False,
                False,
                False,
                False,
                1,
                0.0,
                "",
                False,
            ),
            default=None,
        )
        model.ClearSelection2(True)
        if made_feature is None:
            raise Exception(
                "SolidWorks refused the circular pattern: check the members exist "
                "and the copies don't overlap"
            )
        feature = _flagged(made_feature, "IFeature")
        if centre is None or abs(total - 360.0) < 1e-9:
            break
        target = _rotated(centre, point, axis, math.radians(step))
        if _copy_at(adapter, part, before, target):
            break
        _delete(model, feature)
        feature = None
    if feature is None:
        raise Exception("Neither way round put the copies where asked")
    return feature, "circular", count


def _mirror(
    adapter: Any,
    model: Any,
    fresh: Any,
    mirror: dict[str, Any],
    label: str,
    made: list[str],
) -> tuple[Any, str, int]:
    """Mirror the bodies about a main plane or a plane through a point."""
    planes = main_planes(model)
    if mirror.get("plane"):
        key = str(mirror["plane"]).strip().lower().replace(" plane", "")
        if key not in _PLANE_NAMES:
            raise InputRefused(
                f"mirror.plane {mirror['plane']!r}: give Front, Top or Right (xy, "
                "xz, yz), or a point and normal"
            )
        plane = planes[_PLANE_NAMES[key]]
    else:
        frame = plate_frame(list(mirror["point"]), list(mirror["normal"]))
        features = _flagged(model.FeatureManager, "IFeatureManager")
        axis_line = None
        if main_axis(frame.n) is None:
            # A tilted plane, made the way a tilted plate's is: square to a
            # short line drawn along the normal from the point.
            manager = _flagged(model.SketchManager, "ISketchManager")
            sketch, lines = _draw_axes(
                adapter, model, manager, [frame], True, name=f"{label} mirror axis"
            )
            made.append(sketch)
            axis_line = lines[0]
        plane, new = _plate_plane(
            adapter,
            model,
            features,
            planes,
            frame,
            axis_line,
            label,
            {},
            prefix="Mirror",
        )
        if new:
            made.append(str(plane.Name))
    model.ClearSelection2(True)
    _select_seeds(model, fresh(), False)
    if not _select(("feature", plane), model, True, _MARK_PLANE):
        raise Exception("SolidWorks wouldn't select the mirror plane")
    manager = _flagged(model.FeatureManager, "IFeatureManager")
    made_feature = adapter._attempt(
        lambda: manager.InsertMirrorFeature(True, False, False, False), default=None
    )
    model.ClearSelection2(True)
    if made_feature is None:
        raise Exception("SolidWorks refused the mirror: check the members exist")
    return _flagged(made_feature, "IFeature"), "mirror", 2


def _delete(model: Any, feature: Any) -> None:
    """Delete a feature made a moment ago."""
    model.ClearSelection2(True)
    feature.Select2(False, 0)
    model.EditDelete()
    model.ClearSelection2(True)


def _reversed(ref: tuple[str, Any], d: tuple[float, float, float]) -> bool:
    """Whether a reference axis runs against ``d`` (a pattern follows its first point to its second)."""
    kind, target = ref
    if kind != "feature":
        return False
    try:
        axis = _flagged(_flagged(target, "IFeature").GetSpecificFeature2(), "IRefAxis")
        x1, y1, z1, x2, y2, z2 = (
            float(v) for v in as_list(axis.GetRefAxisParams())[:6]
        )
    except Exception:  # unreadable: the copies are measured anyway
        return False
    return (x2 - x1) * d[0] + (y2 - y1) * d[1] + (z2 - z1) * d[2] < 0


def _skip(
    adapter: Any, model: Any, feature: Any, skip: list[Any], directions: int
) -> None:
    """Leave out instances of a linear pattern (1-based, the seed is 1).

    ``SkippedItemArray`` takes a pair of 0-based indexes (along, along2) per
    instance, as ``VT_I4``; ``AccessSelections`` and ``ModifyDefinition``
    need a ``VT_DISPATCH`` null for the component (seen live: 25 balusters
    less instances 10 and 11 left 23).
    """
    null = _null_callout()
    data = _flagged(feature.GetDefinition(), "ILinearPatternFeatureData")
    data.AccessSelections(model, null)
    pairs: list[int] = []
    for item in skip:
        position = list(item) if isinstance(item, (list, tuple)) else [item]
        if directions == 1:
            position = [position[0], 1]
        pairs.extend([int(position[0]) - 1, int(position[1]) - 1])
    data.SkippedItemArray = long_array(pairs)
    if not feature.ModifyDefinition(data, model, null):
        adapter._attempt(lambda: data.ReleaseSelectionAccess())
        raise Exception(f"SolidWorks wouldn't skip the instances {skip}")
