"""Weldments: structural members on sketch segments, and the cut list.

Checked live on SolidWorks 2025 SP4 before it was written:

- ``IFeatureManager::InsertStructuralWeldment5(path, 1, True, groups, size)``
  takes a profile library part, the size as its configuration name, and one
  ``IStructuralMemberGroup`` per chain of segments (``Segments`` a
  ``VT_DISPATCH`` array). A group whose segments branch (a truss's chords and
  web together) comes back ``None``; chains joined end to end go in, and a
  closed frame of one chain is mitred at every corner.
- The first member needs ``IFeatureManager::InsertWeldmentFeature``
  (``IPartDoc::IsWeldment`` says whether it is there). Inserting a member
  leaves the profile's ``.sldlfp`` open, invisible, so profile parts that
  weren't open before the call are closed again.
- A cut-list folder's resolved ``LENGTH``, ``ANGLE1``, ``ANGLE2`` and
  ``QUANTITY`` come from ``ICustomPropertyManager::Get6`` with byref
  ``VARIANT`` out-parameters; its mass from ``IMassProperty::AddBodies``.
  A 6-panel Warren truss gave 459.23 kg, the part's own mass, split over the
  cut list (2000 mm chords, 1918.3 mm web members mitred 56.31 degrees).
"""

from __future__ import annotations

import csv
import math
import os
import re
from typing import Any, cast

from .. import sw_type_info as _sw_type_info
from ..base import AdapterResult, AdapterResultStatus
from .com_utils import as_list, dispatch_array
from .io import _as_com, _byref_bool, _byref_bstr, _doc_type
from .weldment_library import Point, Profile, ProfileLibrary, chains

#: ``swUserPreferenceStringValue_e.swFileLocationsWeldmentProfiles``.
_SW_WELDMENT_PROFILES = 29
#: ``swUserPreferenceIntegerValue_e.swUnitsLinear``.
_SW_UNITS_LINEAR = 47
#: Millimetres in one ``swLengthUnit_e`` unit.
_MM_PER_UNIT = {
    0: 1.0,
    1: 10.0,
    2: 1000.0,
    3: 25.4,
    4: 304.8,
    5: 304.8,
    6: 1e-7,
    7: 1e-6,
    8: 1e-3,
    9: 0.0254,
    10: 2.54e-5,
}
#: ``swSolidworksWeldmentEndCondOptions_e`` for each corner treatment.
CORNER_TREATMENTS = {"none": 0, "miter": 1, "butt1": 2, "butt2": 3}
#: ``swSolidworksWeldmentEndCondOptions_e`` for each Trim/Extend corner type.
TRIM_CORNERS = {"end_trim": 4, "miter": 1, "butt1": 2, "butt2": 3}
#: ``swWeldmentTrimExtendOptionType_e`` flags.
_TRIM_EXTEND_TRIMMED = 1
_TRIM_EXTEND_TRIMMING = 2
_TRIM_COPED = 4
_TRIM_WELD_GAP = 8
#: ``swBodyType_e.swSolidBody``.
_SOLID_BODY = 0
#: A member body whose volume moved less than this (mm3), and whose centre
#: of mass moved less than ``_UNMOVED_MM``, wasn't trimmed.
_UNCHANGED_MM3 = 1.0
_UNMOVED_MM = 0.001
#: The most body names an error lists.
_LISTED = 20
#: ``swMirrorProfileOrAlignmentAxis_e``.
MIRROR_AXES = {"horizontal": 1, "vertical": 2}
#: ``swConnectedSegmentsOption_e.swConnectedSegments_SimpleCut``.
_SIMPLE_CUT = 1
#: ``swSketchSegments_e``: the segments a structural member can follow.
_SKETCH_LINE, _SKETCH_ARC = 0, 1
#: The cut-list properties read for each item.
_CUT_LIST_FIELDS = (
    "Description",
    "QUANTITY",
    "LENGTH",
    "ANGLE1",
    "ANGLE2",
    "TOTAL LENGTH",
    "MATERIAL",
)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def number(text: str) -> float | None:
    """The first number in a resolved property: ``'56.31°'`` -> 56.31.

    A decimal comma (``'1860,54'``) is read as a point; a thousands comma
    beside a point (``'1,860.54'``) is dropped.
    """
    if "," in text:
        text = text.replace(",", "") if "." in text else text.replace(",", ".")
    match = _NUMBER.search(text)
    return float(match.group()) if match else None


def _feature(obj: Any) -> Any:
    return None if obj is None else _sw_type_info.flagged(obj, "IFeature")


def _need_part(adapter: Any, operation: str) -> AdapterResult[Any] | None:
    """The wrong-document error, or ``None`` when a part is active."""
    if not adapter.currentModel:
        return AdapterResult(status=AdapterResultStatus.ERROR, error="No active model")
    if _doc_type(adapter) == 1:
        return None
    return AdapterResult(
        status=AdapterResultStatus.ERROR,
        error=f"{operation} needs an open part (structural members live in a "
        "weldment part; create_part makes one)",
    )


def _property(adapter: Any, manager: Any, name: str) -> str:
    """A custom property's resolved value through ``Get6`` with byref out-params."""
    stored, resolved = _byref_bstr(), _byref_bstr()
    adapter._attempt(
        lambda: manager.Get6(
            name, False, stored, resolved, _byref_bool(), _byref_bool()
        )
    )
    return str(getattr(resolved, "value", "") or getattr(stored, "value", "") or "")


def _open_profiles(adapter: Any) -> dict[str, str]:
    """The profile library parts open now: lower-case path -> title."""
    found: dict[str, str] = {}
    document = adapter._attempt(lambda: adapter.swApp.GetFirstDocument())
    while document is not None:
        _sw_type_info.flag_members(document, "GetPathName", "GetTitle", "GetNext")
        path = str(
            adapter._attempt(lambda d=document: d.GetPathName(), default="") or ""
        )
        if path.lower().endswith(".sldlfp"):
            found[path.lower()] = str(adapter._attempt(lambda d=document: d.GetTitle()))
        document = adapter._attempt(lambda d=document: d.GetNext())
    return found


def _close_new_profiles(adapter: Any, before: dict[str, str]) -> None:
    """Close the profile parts SolidWorks opened to insert members.

    ``InsertStructuralWeldment5`` leaves the profile's ``.sldlfp`` open and
    invisible (seen live); one the user had open before stays open.
    """
    for path, title in _open_profiles(adapter).items():
        if path not in before and title:
            adapter._attempt(lambda t=title: adapter.swApp.CloseDoc(t))


class SolidWorksWeldmentsMixin:
    """Structural members, the profile library and the cut list."""

    @staticmethod
    def _adapter(obj: Any) -> Any:
        """Return the runtime adapter object for dynamic attribute access."""
        return cast(Any, obj)

    def _profile_library(self) -> ProfileLibrary:
        """The library under the Weldment Profiles file location, kept per folder."""
        adapter = self._adapter(self)
        setting = adapter._attempt(
            lambda: adapter.swApp.GetUserPreferenceStringValue(_SW_WELDMENT_PROFILES),
            default="",
        )
        roots = [p.strip() for p in str(setting or "").split(";") if p.strip()]
        cached = getattr(adapter, "_weldment_library", None)
        if cached is not None and cached.roots == [
            r for r in roots if os.path.isdir(r)
        ]:
            return cast(ProfileLibrary, cached)

        def sizes_of(path: str) -> list[str]:
            names = adapter._attempt(lambda: adapter.swApp.GetConfigurationNames(path))
            return [str(n) for n in as_list(names)]

        library = ProfileLibrary(roots, sizes_of)
        adapter._weldment_library = library
        return library

    async def list_weldment_profiles(
        self,
        standard: str | None = None,
        profile_type: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> AdapterResult[dict[str, Any]]:
        """List the profile library's standards, types or sizes, or search it.

        Args:
            standard: One standard (``DIN``), else every standard.
            profile_type: One type of ``standard`` (``ipe``): its sizes.
            query: Words to find in type and size names (``"HEB400"``).
            limit: The most search results.

        Returns:
            AdapterResult[dict[str, Any]]: ``roots`` and ``standards``,
            ``types`` (with sizes), ``sizes`` or ``matches``.
        """
        adapter = self._adapter(self)

        def run() -> dict[str, Any]:
            library = self._profile_library()
            if not library.roots:
                raise LookupError(
                    "No weldment profile folder: set Tools > Options > File "
                    "Locations > Weldment Profiles in SolidWorks"
                )
            base: dict[str, Any] = {"roots": library.roots}
            if query:
                found = library.search(query, standard, limit + 1)
                return {
                    **base,
                    "query": query,
                    "matches": [p.as_dict() for p in found[:limit]],
                    "truncated": len(found) > limit,
                }
            if standard and profile_type:
                sizes = library.sizes(standard, profile_type)
                return {
                    **base,
                    "standard": standard,
                    "type": profile_type,
                    "sizes": sizes,
                }
            if standard:
                return {
                    **base,
                    "standard": standard,
                    "types": [
                        {"type": t, "sizes": library.sizes(standard, t)}
                        for t in library.types(standard)
                    ],
                }
            return {
                **base,
                "standards": [
                    {"standard": s, "types": library.types(s)}
                    for s in library.standards()
                ],
            }

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("list_weldment_profiles", run),
        )

    async def create_structural_members(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Build one structural member feature: one profile on many segments.

        The segments are either drawn first as a new 3D sketch from
        ``segments`` (``[x1, y1, z1, x2, y2, z2]`` in mm each) or taken from
        an existing sketch (``sketch``, optionally only ``segment_names``).
        They are grouped into chains joined end to end (``grouping`` auto),
        since SolidWorks refuses a group that branches; if it refuses the
        chains too, each segment becomes its own group.

        Args:
            params: ``size`` and either ``profile_path`` or ``standard`` with
                ``profile_type``; ``segments`` or ``sketch``
                (``segment_names``); ``corner_treatment``, ``rotation``,
                ``mirror_profile``, ``merge_arcs``, ``grouping``, ``name``,
                ``sketch_name``.

        Returns:
            AdapterResult[dict[str, Any]]: ``feature``, ``profile``,
            ``sketch``, ``segments``, ``groups``, ``grouping`` and
            ``bodies_after``.
        """
        adapter = self._adapter(self)
        if wrong := _need_part(adapter, "create_structural_members"):
            return wrong

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            profiles_open = _open_profiles(adapter)
            profile = self._resolve_profile(params)
            manager = _sw_type_info.flagged(model.SketchManager, "ISketchManager")
            if adapter._attempt(lambda: manager.ActiveSketch) is not None:
                raise ValueError(
                    "A sketch is open; close it with exit_sketch before adding members"
                )
            if params.get("segments"):
                sketch, raw, ends = _draw_lines(
                    adapter,
                    model,
                    manager,
                    params["segments"],
                    params.get("sketch_name"),
                )
            else:
                sketch, raw, ends = _sketch_segments(
                    adapter, model, str(params["sketch"]), params.get("segment_names")
                )
            features = _sw_type_info.flagged(model.FeatureManager, "IFeatureManager")
            part = _sw_type_info.flagged(model, "IPartDoc")
            if not adapter._attempt(lambda: bool(part.IsWeldment()), default=False):
                features.InsertWeldmentFeature()
            grouping = str(params.get("grouping") or "auto")
            plans = {
                "single": [list(range(len(raw)))],
                "each": [[i] for i in range(len(raw))],
                "auto": chains(ends),
            }
            feature = _insert_members(
                adapter, features, profile, raw, plans[grouping], params
            )
            if feature is None and grouping == "auto" and len(plans["auto"]) < len(raw):
                grouping = "each"
                feature = _insert_members(
                    adapter, features, profile, raw, plans["each"], params
                )
            _close_new_profiles(adapter, profiles_open)
            if feature is None:
                raise Exception(
                    f"SolidWorks refused the structural member ({profile.type} "
                    f"{profile.size} on {len(raw)} segment(s) of {sketch}). A group "
                    "must be segments joined end to end; try grouping 'each', and "
                    "check the segments don't overlap."
                )
            if params.get("name"):
                feature.Name = str(params["name"])
            bodies = as_list(
                adapter._attempt(lambda: part.GetBodies2(0, True), default=None)
            )
            adapter._attempt(lambda: model.GraphicsRedraw2())
            return {
                "feature": str(feature.Name),
                "profile": profile.as_dict(),
                "sketch": sketch,
                "segments": len(raw),
                "groups": len(plans[grouping]),
                "grouping": grouping,
                "bodies_after": len(bodies),
            }

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("create_structural_members", run),
        )

    async def create_trim_extend(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Trim or extend members to a boundary: SolidWorks' Trim/Extend feature.

        ``IFeatureManager::InsertWeldmentTrimFeature2(corner, options, gap,
        bodies, boundary)`` with the bodies as ``VT_DISPATCH`` arrays (checked
        live on SolidWorks 2025 SP4: seven IPE160 joists coped to two IPE240
        beams with a 5 mm gap came out 3983.8 mm, the 4000 mm between beam
        centres less the 6.2 mm web and both gaps). Names are structural
        member features (all their bodies) or bodies (``Joists[3]``). Each
        member body's volume is read before and after, so one the trim
        didn't reach is reported; SolidWorks renames the bodies it trims,
        so each member's new name is given too (:func:`_trimmed`).

        Args:
            params: ``members`` and ``boundary`` (names), ``corner_type``
                (``end_trim``, ``miter``, ``butt1``, ``butt2``), ``cut``
                (``simple`` or ``coped``), ``weld_gap_mm``,
                ``allow_extension`` and ``name``.

        Returns:
            AdapterResult[dict[str, Any]]: ``feature``, ``corner_type``,
            ``cut``, ``weld_gap_mm``, ``members`` (each body's ``name``,
            ``now`` and ``volume_change_mm3``), ``boundary`` (body names),
            ``unchanged`` and ``bodies_after``.
        """
        adapter = self._adapter(self)
        if wrong := _need_part(adapter, "create_trim_extend"):
            return wrong
        corner = str(params.get("corner_type") or "end_trim")
        cut = str(params.get("cut") or "simple")
        gap_mm = float(params.get("weld_gap_mm") or 0.0)

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            part = _sw_type_info.flagged(model, "IPartDoc")
            members = _named_bodies(adapter, model, part, params.get("members") or [])
            boundary = _named_bodies(adapter, model, part, params.get("boundary") or [])
            both = sorted(set(members) & set(boundary))
            if both:
                raise ValueError(
                    f"{', '.join(both)} can't be both trimmed and the boundary"
                )
            if corner != "end_trim" and (len(members), len(boundary)) != (1, 1):
                raise ValueError(
                    f"A {corner} corner joins two members: give one member body and "
                    f"one boundary body (got {len(members)} and {len(boundary)})"
                )
            options = _TRIM_COPED if cut == "coped" else 0
            if params.get("allow_extension", True):
                options |= _TRIM_EXTEND_TRIMMED
                if corner != "end_trim":
                    options |= _TRIM_EXTEND_TRIMMING
            if gap_mm > 0:
                options |= _TRIM_WELD_GAP
            existing = set(_bodies(adapter, part))
            before = {name: _mass(adapter, body) for name, body in members.items()}
            manager = _sw_type_info.flagged(model.FeatureManager, "IFeatureManager")
            feature = _feature(
                adapter._attempt(
                    lambda: manager.InsertWeldmentTrimFeature2(
                        TRIM_CORNERS[corner],
                        options,
                        gap_mm / 1000.0,
                        dispatch_array(list(members.values())),
                        dispatch_array(list(boundary.values())),
                    )
                )
            )
            if feature is None:
                raise Exception(
                    "SolidWorks refused the Trim/Extend: check each member reaches "
                    "the boundary or points at it (allow_extension extends it), and "
                    "that a miter or butt corner names two members that meet"
                )
            if params.get("name"):
                feature.Name = str(params["name"])
            after = _bodies(adapter, part)
            changes = _trimmed(
                before,
                {name: _mass(adapter, body) for name, body in after.items()},
                existing,
            )
            unchanged = [c["name"] for c in changes if c["changed"] is False]
            data: dict[str, Any] = {
                "feature": str(feature.Name),
                "corner_type": corner,
                "cut": cut,
                "weld_gap_mm": gap_mm,
                "members": changes,
                "boundary": list(boundary),
                "unchanged": unchanged,
                "bodies_after": len(after),
            }
            if unchanged:
                data["warning"] = (
                    f"{len(unchanged)} member(s) didn't change: {', '.join(unchanged)}; "
                    "the boundary may not reach them."
                )
            return data

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("create_trim_extend", run),
        )

    def _resolve_profile(self, params: dict[str, Any]) -> Profile:
        """The profile ``params`` name: a library entry, or a file and size."""
        size = str(params.get("size") or "")
        path = params.get("profile_path")
        if path:
            path = os.path.abspath(str(path))
            if not os.path.isfile(path):
                raise ValueError(f"Profile file not found: {path}")
            adapter = self._adapter(self)
            sizes = [
                str(n)
                for n in as_list(
                    adapter._attempt(lambda: adapter.swApp.GetConfigurationNames(path))
                )
            ]
            if size and sizes and size not in sizes:
                raise ValueError(
                    f"{os.path.basename(path)} has no size {size!r}; it has "
                    f"{', '.join(sizes)}"
                )
            stem = os.path.splitext(os.path.basename(path))[0]
            return Profile("custom", stem, size or stem, path, size)
        return self._profile_library().resolve(
            str(params.get("standard") or ""),
            str(params.get("profile_type") or ""),
            size,
        )

    async def get_cut_list(
        self, update: bool = True, export_path: str | None = None
    ) -> AdapterResult[dict[str, Any]]:
        """Read the active weldment's cut list: items, lengths, angles, masses.

        Args:
            update: Update the cut list first (``IBodyFolder::UpdateCutList``).
            export_path: Also write it to this ``.csv`` file.

        Returns:
            AdapterResult[dict[str, Any]]: ``items`` (``item``, ``name``,
            ``description``, ``quantity``, ``length_mm``, ``angle1_deg``,
            ``angle2_deg``, ``total_length_mm`` (length times quantity),
            ``profile_total_length_mm`` (every item of that profile, as
            SolidWorks adds it up), ``material``, ``mass_each_kg``,
            ``mass_total_kg``) and the totals.
        """
        adapter = self._adapter(self)
        if wrong := _need_part(adapter, "get_cut_list"):
            return wrong
        if export_path and not str(export_path).lower().endswith(".csv"):
            return AdapterResult(
                status=AdapterResultStatus.ERROR,
                error="export_path must end in .csv",
            )

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            unit = adapter._attempt(
                lambda: int(model.GetUserPreferenceIntegerValue(_SW_UNITS_LINEAR)),
                default=0,
            )
            to_mm = _MM_PER_UNIT.get(int(unit or 0), 1.0)
            extension = _sw_type_info.flagged(model.Extension, "IModelDocExtension")
            items = []
            for index, folder in enumerate(
                _cut_list_folders(adapter, model, update), 1
            ):
                items.append(_cut_list_item(adapter, extension, folder, index, to_mm))
            if not items:
                raise LookupError(
                    "The part has no cut list: it is not a weldment. "
                    "create_structural_members makes one."
                )
            data: dict[str, Any] = {
                "items": items,
                "item_count": len(items),
                "pieces": sum(i["quantity"] for i in items),
                "total_length_mm": round(
                    sum(i["total_length_mm"] or 0.0 for i in items), 3
                ),
                "total_mass_kg": round(
                    sum(i["mass_total_kg"] or 0.0 for i in items), 3
                ),
            }
            if any("not specified" in str(i["material"]).lower() for i in items):
                data["warning"] = (
                    "Some items have no material, so their mass uses SolidWorks' "
                    "default density; set_material assigns one."
                )
            if export_path:
                data["export_path"] = _write_csv(str(export_path), items)
            return data

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("get_cut_list", run),
        )


def _draw_lines(
    adapter: Any,
    model: Any,
    manager: Any,
    segments: list[list[float]],
    name: str | None,
) -> tuple[str, list[Any], list[tuple[Point, Point]]]:
    """A new 3D sketch of one line per segment (mm), closed again.

    Returns:
        tuple: The sketch's name, its segments (raw dispatches, in order)
        and each segment's ends in mm.
    """
    manager.AddToDB = True
    manager.Insert3DSketch(True)
    try:
        for x1, y1, z1, x2, y2, z2 in segments:
            line = manager.CreateLine(
                x1 / 1000.0,
                y1 / 1000.0,
                z1 / 1000.0,
                x2 / 1000.0,
                y2 / 1000.0,
                z2 / 1000.0,
            )
            if line is None:
                raise ValueError(
                    f"SolidWorks refused the line {[x1, y1, z1]} -> {[x2, y2, z2]} mm"
                )
    finally:
        manager.Insert3DSketch(True)
        manager.AddToDB = False
    feature = _feature(model.FeatureByPositionReverse(0))
    if name:
        feature.Name = str(name)
    sketch = _sw_type_info.flagged(feature.GetSpecificFeature2(), "ISketch")
    raw = as_list(sketch.GetSketchSegments())
    if len(raw) != len(segments):
        raise Exception(
            f"The 3D sketch holds {len(raw)} segment(s) for {len(segments)} line(s); "
            "two lines may be the same line"
        )
    ends = [((s[0], s[1], s[2]), (s[3], s[4], s[5])) for s in segments]
    return str(feature.Name), raw, ends


def _sketch_segments(
    adapter: Any, model: Any, sketch: str, names: list[str] | None
) -> tuple[str, list[Any], list[tuple[Point, Point]]]:
    """An existing sketch's lines and arcs (construction lines left out).

    Returns:
        tuple: The sketch's name, the chosen segments (raw dispatches) and
        their ends in the sketch's own coordinates.

    Raises:
        LookupError: No such sketch, or a named segment isn't in it.
    """
    feature = _feature(adapter._attempt(lambda: model.FeatureByName(sketch)))
    if feature is None:
        raise LookupError(f"No sketch named {sketch!r} in the part")
    found = _sw_type_info.flagged(feature.GetSpecificFeature2(), "ISketch")
    wanted = set(names or [])
    raw: list[Any] = []
    ends: list[tuple[Point, Point]] = []
    seen: list[str] = []
    for segment in as_list(found.GetSketchSegments()):
        wrapped = _as_com(adapter, segment, "ISketchSegment")
        if wrapped is None or bool(wrapped.ConstructionGeometry):
            continue
        kind = int(wrapped.GetType())
        if kind not in (_SKETCH_LINE, _SKETCH_ARC):
            continue
        label = str(wrapped.GetName())
        seen.append(label)
        if wanted and label not in wanted:
            continue
        shape = _sw_type_info.flagged(
            wrapped, "ISketchLine" if kind == _SKETCH_LINE else "ISketchArc"
        )
        points = [
            _sw_type_info.flagged(p, "ISketchPoint")
            for p in (shape.GetStartPoint2(), shape.GetEndPoint2())
        ]
        start, end = ((p.X * 1000.0, p.Y * 1000.0, p.Z * 1000.0) for p in points)
        raw.append(segment)
        ends.append((start, end))
    missing = sorted(wanted - set(seen))
    if missing:
        raise LookupError(
            f"{sketch} has no segment(s) {', '.join(missing)}; it has "
            f"{', '.join(seen) or 'no lines or arcs'}"
        )
    if not raw:
        raise LookupError(f"{sketch} has no lines or arcs to put members on")
    return sketch, raw, ends


def _insert_members(
    adapter: Any,
    features: Any,
    profile: Profile,
    raw: list[Any],
    plan: list[list[int]],
    params: dict[str, Any],
) -> Any:
    """``InsertStructuralWeldment5`` with one group per chain; the feature or None."""
    treatment = CORNER_TREATMENTS[str(params.get("corner_treatment") or "miter")]
    mirror = str(params.get("mirror_profile") or "none")
    groups = []
    for chain in plan:
        group = _sw_type_info.flagged(
            features.CreateStructuralMemberGroup(), "IStructuralMemberGroup"
        )
        group.Segments = dispatch_array([raw[i] for i in chain])
        group.ApplyCornerTreatment = treatment != 0
        if treatment:
            group.CornerTreatmentType = treatment
        group.Angle = math.radians(float(params.get("rotation") or 0.0))
        if mirror in MIRROR_AXES:
            group.MirrorProfile = True
            group.MirrorProfileAxis = MIRROR_AXES[mirror]
        group.MergeArcSegmentBodies = bool(params.get("merge_arcs"))
        groups.append(group)
    inserted = adapter._attempt(
        lambda: features.InsertStructuralWeldment5(
            profile.path,
            _SIMPLE_CUT,
            True,
            dispatch_array(groups),
            profile.configuration,
        )
    )
    return _feature(inserted)


def _bodies(adapter: Any, part: Any) -> dict[str, Any]:
    """The part's solid bodies by name, hidden ones too."""
    found: dict[str, Any] = {}
    for raw in as_list(
        adapter._attempt(lambda: part.GetBodies2(_SOLID_BODY, False), default=None)
    ):
        body = _sw_type_info.flagged(raw, "IBody2")
        found[str(body.Name)] = body
    return found


#: A body's centre of mass (m) and volume (m3), or ``None`` when unreadable.
Mass = tuple[tuple[float, float, float], float] | None


def _mass(adapter: Any, body: Any) -> Mass:
    """A body's centre of mass and volume (``IBody2::GetMassProperties``)."""
    values = as_list(
        adapter._attempt(lambda: body.GetMassProperties(1.0), default=None)
    )
    if len(values) < 4:
        return None
    return (float(values[0]), float(values[1]), float(values[2])), float(values[3])


def _trimmed(
    before: dict[str, Mass], after: dict[str, Mass], existing: set[str]
) -> list[dict[str, Any]]:
    """What a Trim/Extend did to each member body.

    SolidWorks renames the bodies it trims after the feature (``Joists[1]``
    becomes ``Trim/Extend1[1]``, seen live), so a member whose name is gone
    is matched to the new body whose centre of mass is nearest its own: a
    trim moves the centre by a fraction of the cut, members lie far apart.

    Args:
        before: Each member body's mass, by its name before the trim.
        after: Every body's mass after the trim, by name.
        existing: Every body's name before the trim.

    Returns:
        list[dict[str, Any]]: Each member's ``name``, ``now`` (its name after
        the trim), ``volume_change_mm3`` and ``changed`` (``None`` when
        unreadable). A mitre keeps the volume (the corner gained outside is
        the corner cut inside) but moves the centre of mass, so either
        counts as a change.
    """
    fresh: dict[str, tuple[tuple[float, float, float], float]] = {
        n: m for n, m in after.items() if n not in existing and m is not None
    }
    changes = []
    for name, mass in before.items():
        now: str | None = name if name in after else None
        if now is None and mass is not None and fresh:
            centre = mass[0]
            now = min(fresh, key=lambda n: math.dist(centre, fresh[n][0]))
            fresh.pop(now)
        moved = after.get(now) if now else None
        if mass is None or moved is None:
            change, changed = None, None
        else:
            change = (moved[1] - mass[1]) * 1e9
            shift = math.dist(moved[0], mass[0]) * 1000.0
            changed = abs(change) >= _UNCHANGED_MM3 or shift >= _UNMOVED_MM
        changes.append(
            {
                "name": name,
                "now": now,
                "volume_change_mm3": None if change is None else round(change, 3),
                "changed": changed,
            }
        )
    return changes


def _feature_bodies(adapter: Any, feature: Any) -> list[str]:
    """The names of the bodies a feature made, from the faces it made."""
    names: list[str] = []
    for raw in as_list(adapter._attempt(lambda: feature.GetFaces(), default=None)):
        face = _sw_type_info.flagged(raw, "IFace2")
        name = adapter._attempt(
            lambda f=face: str(_sw_type_info.flagged(f.GetBody(), "IBody2").Name),
            default="",
        )
        if name and name not in names:
            names.append(name)
    return names


def _member_features(model: Any) -> list[str]:
    """The structural member features' names, in tree order."""
    names: list[str] = []
    feature = _feature(model.FirstFeature())
    while feature is not None:
        if feature.GetTypeName2() == "WeldMemberFeat":
            names.append(str(feature.Name))
        feature = _feature(feature.GetNextFeature())
    return names


def _named_bodies(
    adapter: Any, model: Any, part: Any, names: list[str]
) -> dict[str, Any]:
    """The bodies ``names`` name: a body by its name, or every body of a feature.

    Raises:
        LookupError: A name that is neither a body nor a feature with bodies;
            the message lists the members and (when few) the bodies.
    """
    bodies = _bodies(adapter, part)
    chosen: dict[str, Any] = {}
    for name in names:
        if name in bodies:
            chosen[name] = bodies[name]
            continue
        feature = _feature(adapter._attempt(lambda n=name: model.FeatureByName(n)))
        owned = (
            [b for b in _feature_bodies(adapter, feature) if b in bodies]
            if (feature is not None)
            else []
        )
        if not owned:
            listed = (
                f"; its bodies are {', '.join(bodies)}"
                if len(bodies) <= _LISTED
                else f" and {len(bodies)} bodies, named '<member>[n]'"
            )
            raise LookupError(
                f"No structural member or body named {name!r}; the part has the "
                f"members {', '.join(_member_features(model)) or 'none'}{listed}"
            )
        for body_name in owned:
            chosen[body_name] = bodies[body_name]
    return chosen


def _cut_list_folders(adapter: Any, model: Any, update: bool) -> list[Any]:
    """The cut-list folders under the part's Solid Bodies folder, in tree order."""
    folders: list[Any] = []
    feature = _feature(model.FirstFeature())
    while feature is not None:
        if feature.GetTypeName2() == "SolidBodyFolder":
            if update:
                body_folder = _sw_type_info.flagged(
                    feature.GetSpecificFeature2(), "IBodyFolder"
                )
                adapter._attempt(lambda f=body_folder: f.UpdateCutList())
            sub = _feature(feature.GetFirstSubFeature())
            while sub is not None:
                if sub.GetTypeName2() == "CutListFolder":
                    folders.append(sub)
                sub = _feature(sub.GetNextSubFeature())
            return folders
        feature = _feature(feature.GetNextFeature())
    return folders


def _cut_list_item(
    adapter: Any, extension: Any, folder: Any, index: int, to_mm: float
) -> dict[str, Any]:
    """One cut-list item: its properties, piece count and mass."""
    manager = _sw_type_info.flagged(
        folder.CustomPropertyManager, "ICustomPropertyManager"
    )
    values = {name: _property(adapter, manager, name) for name in _CUT_LIST_FIELDS}
    body_folder = _sw_type_info.flagged(folder.GetSpecificFeature2(), "IBodyFolder")
    bodies = as_list(adapter._attempt(lambda: body_folder.GetBodies()))
    quantity = int(number(values["QUANTITY"]) or len(bodies))
    mass = None
    if bodies:
        properties = _sw_type_info.flagged(
            extension.CreateMassProperty(), "IMassProperty"
        )
        if adapter._attempt(lambda: properties.AddBodies(dispatch_array(bodies))):
            mass = float(properties.Mass)
    length = number(values["LENGTH"])
    # SolidWorks' TOTAL LENGTH is every item of the same profile together
    # (seen live: 22000 on both chords of 12000 and 10000), so the item's own
    # total is its length times its quantity.
    profile_total = number(values["TOTAL LENGTH"])
    return {
        "item": index,
        "name": str(folder.Name),
        "description": values["Description"],
        "quantity": quantity,
        "length_mm": None if length is None else round(length * to_mm, 3),
        "angle1_deg": number(values["ANGLE1"]),
        "angle2_deg": number(values["ANGLE2"]),
        "total_length_mm": None
        if length is None
        else round(length * to_mm * quantity, 3),
        "profile_total_length_mm": (
            None if profile_total is None else round(profile_total * to_mm, 3)
        ),
        "material": values["MATERIAL"],
        "mass_each_kg": None if mass is None else round(mass / max(quantity, 1), 3),
        "mass_total_kg": None if mass is None else round(mass, 3),
    }


def _write_csv(path: str, items: list[dict[str, Any]]) -> str:
    """Write the cut list as CSV; return the absolute path."""
    out = os.path.abspath(path)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(items[0]))
        writer.writeheader()
        writer.writerows(items)
    return out
