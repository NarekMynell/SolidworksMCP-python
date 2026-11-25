"""Cuts in weldment members in one call: bolt holes, copes and notches.

A cut is drawn like a plate: an outline (a cope, a notch, an opening) and
holes in its own x/y, placed by an origin, a normal and an in-plane x axis.
It goes ``depth`` through, centred on that plane, so it reaches what lies
there and nothing further: a joist's web and the fin plate bolted to it, the
top flange and web at a beam's end. Its sketch is tied to the origin (fully
defined) on a plane tied to the main planes, the cut is named, and the bodies
it went through are listed, so a cut that reached the wrong member shows.

Holes a plate carries itself belong in ``create_plates``; these are the holes
and copes in the members.
"""

from __future__ import annotations

import math
from typing import Any, cast

from ...exceptions import InputRefused
from .. import sw_type_info as _sw_type_info
from ..base import AdapterResult
from .com_utils import as_list
from .feature_tree import move_into_folder
from .sketch_define import main_planes
from .weldment_plates import (
    _area,
    _draw_axes,
    _free_name,
    _inside,
    _plate_plane,
    _plate_sketch,
    drop_stray_planes,
    main_axis,
    plane_names,
    plate_frame,
)
from .weldments import _feature_bodies, _need_part

#: ``swEndConditions_e``.
_END_BLIND, _END_MID_PLANE = 0, 6


def _flagged(obj: Any, interface: str) -> Any:
    return _sw_type_info.flagged(obj, interface)


def check_cut(spec: dict[str, Any]) -> list[tuple[float, float]]:
    """Check a cut's outline and holes; its outline (empty for holes only).

    Raises:
        ValueError: Nothing to cut, an outline with no area or a repeated
            corner, a hole inside the outline (SolidWorks would leave it
            standing as an island), or holes that overlap.
    """
    label = spec.get("name") or "cut"
    points: list[tuple[float, float]] = []
    if spec.get("outline"):
        points = [(float(p[0]), float(p[1])) for p in spec["outline"]]
    elif spec.get("width") and spec.get("height"):
        w, h = float(spec["width"]) / 2.0, float(spec["height"]) / 2.0
        points = [(-w, -h), (w, -h), (w, h), (-w, h)]
    holes = [
        (float(p[0]), float(p[1]), float(hole["diameter"]) / 2.0)
        for hole in spec.get("holes") or []
        for p in hole["positions"]
    ]
    if not points and not holes:
        raise ValueError(
            f"{label}: give an outline (or width and height), holes, or both"
        )
    if points:
        for a, b in zip(points, points[1:] + points[:1], strict=True):
            if math.dist(a, b) < 1e-6:
                raise ValueError(f"{label}: the outline repeats the corner {list(a)}")
        if abs(_area(points)) < 1.0:
            raise ValueError(f"{label}: the outline has no area")
    for x, y, r in holes:
        if points and _inside((x, y), points):
            raise ValueError(
                f"{label}: the Ø{2 * r:g} hole at {[x, y]} is inside the outline; "
                "the outline already removes that steel"
            )
    for i, (x1, y1, r1) in enumerate(holes):
        for x2, y2, r2 in holes[i + 1 :]:
            if math.hypot(x2 - x1, y2 - y1) < r1 + r2:
                raise ValueError(
                    f"{label}: the holes at {[x1, y1]} and {[x2, y2]} overlap"
                )
    return points


class SolidWorksWeldmentCutsMixin:
    """Bolt holes, copes and notches in weldment members."""

    @staticmethod
    def _adapter(obj: Any) -> Any:
        """Return the runtime adapter object for dynamic attribute access."""
        return cast(Any, obj)

    async def create_cuts(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Cut holes, copes and notches, each through ``depth`` centred on its plane.

        Args:
            params: ``cuts`` (each ``name``, ``origin``, ``normal``,
                ``depth`` and ``outline`` or ``width`` and ``height``,
                ``holes`` or both, and optionally ``x_axis``), ``folder``
                and ``define``.

        Returns:
            AdapterResult[dict[str, Any]]: ``cuts`` (each ``name``,
            ``feature``, ``sketch``, ``plane``, ``holes``, ``bodies`` and
            ``definition``), ``planes_made`` and ``folder``.
        """
        adapter = self._adapter(self)
        if wrong := _need_part(adapter, "create_cuts"):
            return wrong
        specs = list(params["cuts"])
        define = str(params.get("define") or "dimensions") == "dimensions"
        folder = params.get("folder")

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            outlines = [check_cut(spec) for spec in specs]
            frames = [
                plate_frame(spec["origin"], spec["normal"], spec.get("x_axis"))
                for spec in specs
            ]
            manager = _flagged(model.SketchManager, "ISketchManager")
            if adapter._attempt(lambda: manager.ActiveSketch) is not None:
                raise ValueError(
                    "A sketch is open; close it with exit_sketch before cutting"
                )
            features = _flagged(model.FeatureManager, "IFeatureManager")
            planes_before = adapter._attempt(lambda: plane_names(model), default=set())
            planes = main_planes(model)
            made_names: list[str] = []
            tilted = [i for i, f in enumerate(frames) if main_axis(f.n) is None]
            axis_lines: dict[int, tuple[Any, Any]] = {}
            if tilted:
                sketch_name, lines = _draw_axes(
                    adapter,
                    model,
                    manager,
                    [frames[i] for i in tilted],
                    define,
                    name="Cut axes",
                )
                made_names.append(sketch_name)
                axis_lines = dict(zip(tilted, lines, strict=True))
            shared: dict[tuple[int, float], Any] = {}
            results: list[dict[str, Any]] = []
            planes_made = 0
            for index, (spec, frame, points) in enumerate(
                zip(specs, frames, outlines, strict=True)
            ):
                label = str(spec["name"]).strip()
                plane, new_plane = _plate_plane(
                    adapter,
                    model,
                    features,
                    planes,
                    frame,
                    axis_lines.get(index),
                    label,
                    shared,
                    prefix="Cuts",
                )
                if new_plane:
                    planes_made += 1
                    made_names.append(str(plane.Name))
                sketch, state = _plate_sketch(
                    adapter, model, manager, plane, frame, spec, points, define, label
                )
                feature, bodies = _cut_through(
                    adapter, model, features, sketch, float(spec["depth"]), label
                )
                made_names.append(feature)
                results.append(
                    {
                        "name": label,
                        "feature": feature,
                        "sketch": sketch,
                        "plane": str(plane.Name),
                        "holes": sum(
                            len(h["positions"]) for h in spec.get("holes") or []
                        ),
                        "bodies": bodies,
                        "definition": state,
                    }
                )
            # Each cut renames the bodies it touches, so the names read as it
            # was made are stale by the end (one tube cut 18 times read as 18
            # bodies): each cut's bodies are read again from its faces.
            for entry in results:
                feature = adapter._attempt(
                    lambda e=entry: model.FeatureByName(e["feature"])
                )
                if feature is not None:
                    entry["bodies"] = sorted(
                        _feature_bodies(
                            adapter, _sw_type_info.flagged(feature, "IFeature")
                        )
                    )
            stray = adapter._attempt(
                lambda: drop_stray_planes(adapter, model, planes_before), default=[]
            )
            placed = None
            if folder:
                placed = move_into_folder(adapter, model, str(folder), made_names)
            adapter._attempt(lambda: model.GraphicsRedraw2())
            data: dict[str, Any] = {
                "cuts": results,
                "planes_made": planes_made,
                "folder": placed,
            }
            if stray:
                data["stray_planes_removed"] = stray
            return data

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("create_cuts", run),
        )


def _cut_through(
    adapter: Any,
    model: Any,
    features: Any,
    sketch: str,
    depth_mm: float,
    label: str,
) -> tuple[str, list[str]]:
    """Cut the sketch through ``depth_mm`` centred on its plane; the cut and the bodies it cut.

    Raises:
        InputRefused: SolidWorks made no cut: nothing lies within the depth.
    """
    sketch_feature = _flagged(model.FeatureByName(sketch), "IFeature")
    model.ClearSelection2(True)
    sketch_feature.Select2(False, 0)
    made = features.FeatureCut4(
        True,
        False,
        False,
        _END_MID_PLANE,
        _END_BLIND,
        depth_mm / 1000.0,
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
    if made is None:
        raise InputRefused(
            f"SolidWorks made no cut for {label}: no steel within {depth_mm:g} mm "
            "of its plane inside the outline or holes. Check origin (on the "
            "member, e.g. the web's middle), normal and depth."
        )
    feature = _flagged(made, "IFeature")
    feature.Name = _free_name(model, label)
    return str(feature.Name), _bodies_of(adapter, feature)


def _bodies_of(adapter: Any, feature: Any) -> list[str]:
    """The names of the bodies a cut went through, read from its faces."""
    names: set[str] = set()
    for raw in as_list(adapter._attempt(lambda: feature.GetFaces(), default=None)):
        face = _flagged(raw, "IFace2")
        body = adapter._attempt(lambda f=face: _flagged(f.GetBody(), "IBody2"))
        if body is not None:
            names.add(str(body.Name))
    return sorted(names)
