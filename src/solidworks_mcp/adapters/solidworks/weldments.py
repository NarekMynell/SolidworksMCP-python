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
import time
from typing import Any, cast

from ...exceptions import InputRefused
from .. import sw_type_info as _sw_type_info
from ..base import AdapterResult, AdapterResultStatus
from . import feature_tree as _tree
from . import sketch_define as _define
from . import weldment_joints as _joints
from . import weldment_orientation as _orient
from .com_utils import as_list, dispatch_array
from .features import _null_callout
from .io import (
    _as_com,
    _byref_bool,
    _byref_bstr,
    _byref_double,
    _byref_variant,
    _doc_type,
)
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
#: Settle pauses (``SOLIDWORKS_MCP_COM_SETTLE_SECONDS``, 0.25 s) waited before
#: asking SolidWorks again for refused members: 1 s by default, none in tests.
_REFUSED_RETRY_SETTLES = 4.0
#: A new body at least this share of a trimmed member's volume can be that member.
_SAME_MEMBER_SHARE = 0.5
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
            skeleton: dict[str, Any] | None = None
            part = _sw_type_info.flagged(model, "IPartDoc")
            grouping = str(params.get("grouping") or "auto")
            chain_plan: list[list[int]] | None = None
            in_model_space = True
            if params.get("segments"):
                segments = [[float(v) for v in s] for s in params["segments"]]
                if grouping == "auto":
                    # Drawn chain by chain, head to tail: the way SolidWorks
                    # carries the profile round a chain's bends.
                    ends_now = _ends_of(segments)
                    segments, chain_plan = _orient.chained(
                        segments,
                        chains(ends_now, stops=_member_nodes(adapter, part, ends_now)),
                    )
                sketch_name = params.get("sketch_name") or (
                    f"{params['name']} skeleton" if params.get("name") else None
                )
                sketch, raw, ends, skeleton = _draw_lines(
                    adapter,
                    model,
                    manager,
                    segments,
                    sketch_name,
                    define=str(params.get("define") or "dimensions") == "dimensions",
                )
            else:
                sketch, raw, ends, in_model_space = _sketch_segments(
                    adapter, model, str(params["sketch"]), params.get("segment_names")
                )
            features = _sw_type_info.flagged(model.FeatureManager, "IFeatureManager")
            if not adapter._attempt(lambda: bool(part.IsWeldment()), default=False):
                features.InsertWeldmentFeature()
            plans = {
                "single": [list(range(len(raw)))],
                "each": [[i] for i in range(len(raw))],
                "auto": chain_plan
                or chains(ends, stops=_member_nodes(adapter, part, ends)),
            }
            facing = _Facing(params, ends, raw, in_model_space)
            feature = _insert_members(
                adapter,
                features,
                profile,
                raw,
                plans[grouping],
                params,
                facing.angles(plans[grouping]),
            )
            if feature is None and grouping == "auto" and len(plans["auto"]) < len(raw):
                grouping = "each"
                feature = _insert_members(
                    adapter,
                    features,
                    profile,
                    raw,
                    plans["each"],
                    params,
                    facing.angles(plans["each"]),
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
            placed = None
            if params.get("folder"):
                placed = _tree.move_into_folder(
                    adapter, model, str(params["folder"]), [sketch, str(feature.Name)]
                )
            bodies = as_list(
                adapter._attempt(lambda: part.GetBodies2(0, True), default=None)
            )
            adapter._attempt(lambda: model.GraphicsRedraw2())
            result: dict[str, Any] = {
                "feature": str(feature.Name),
                "profile": profile.as_dict(),
                "sketch": sketch,
                "segments": len(raw),
                "groups": len(plans[grouping]),
                "grouping": grouping,
                "bodies_after": len(bodies),
                "orientation": facing.report(adapter, part, feature, plans[grouping]),
            }
            if skeleton is not None:
                result["skeleton"] = skeleton
            if placed is not None:
                result["folder"] = placed
            return result

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
            folder = params.get("folder") or _tree.last_folder_of(
                model, [*params.get("members", []), *params.get("boundary", [])]
            )
            placed = (
                _tree.move_into_folder(adapter, model, str(folder), [str(feature.Name)])
                if folder
                else None
            )
            after = _bodies(adapter, part)
            masses = {name: _mass(adapter, body) for name, body in after.items()}
            changes = _trimmed(before, masses, existing)
            taken = {c["now"] for c in changes if c["now"]}
            extra = [n for n in after if n not in existing and n not in taken]
            offcuts, split = _offcuts(
                adapter, after, extra, [after[n] for n in boundary if n in after]
            )
            dropped = None
            if offcuts:
                dropped = _delete_bodies(
                    adapter, model, after, offcuts, f"{feature.Name} leftovers"
                )
                after = _bodies(adapter, part)
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
                "folder": placed,
            }
            if offcuts:
                data["leftovers_removed"] = {"count": len(offcuts), "feature": dropped}
            notes = []
            if unchanged:
                notes.append(
                    f"{len(unchanged)} member(s) didn't change: {', '.join(unchanged)}; "
                    "they already end at it (trimmed to a member of their own "
                    "call where they meet), or it doesn't reach them: check_joints "
                    "tells which"
                )
            if split:
                data["split_pieces"] = split[:_LISTED]
                notes.append(
                    f"{len(split)} new body(ies) {', '.join(split[:5])}: a member ran "
                    "through the boundary and was cut in two. Members that end at a "
                    "column or beam are drawn to it (grouping 'each'), not through it"
                )
            if notes:
                data["warning"] = "; ".join(notes) + "."
            return data

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("create_trim_extend", run),
        )

    async def check_joints(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Measure a weldment's joints: overlapping bodies, open ends, loose bodies.

        See :mod:`.weldment_joints` for how each is measured and what it
        costs (about 0.2 s a body, and 0.1 s for each pair of bodies whose
        boxes overlap).

        Args:
            params: ``members`` (names: only those bodies), ``overlap_mm3``,
                ``contact_mm`` and ``max_listed``.

        Returns:
            AdapterResult[dict[str, Any]]: ``bodies``, ``checked_bodies``,
            ``members``, ``overlaps``, ``open_ends``, ``partly_open_ends``,
            ``loose``, ``loose_count``, ``groups``, ``group_sizes``,
            ``clean``, ``pairs_checked`` and ``seconds``.
        """
        adapter = self._adapter(self)
        if wrong := _need_part(adapter, "check_joints"):
            return wrong

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            part = _sw_type_info.flagged(model, "IPartDoc")
            only = None
            if params.get("members"):
                only = set(_named_bodies(adapter, model, part, list(params["members"])))
            modeler = adapter._attempt(lambda: adapter.swApp.GetModeler(), default=None)
            if modeler is None:
                raise Exception("SolidWorks gave no modeler to measure the bodies with")
            return _joints.check(
                adapter,
                part,
                _sw_type_info.flagged(modeler, "IModeler"),
                _byref_variant,
                float(params.get("overlap_mm3", 1.0)),
                float(params.get("contact_mm", 0.5)),
                int(params.get("max_listed", 25)),
                only,
                _byref_double,
            )

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("check_joints", run),
        )

    async def create_end_caps(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Close member ends with SolidWorks' End Cap feature.

        The ends are the member end faces nearest the given points, or every
        end :func:`.weldment_joints.contacts` finds touching nothing
        (``open_ends``). One End Cap feature caps them all.

        Args:
            params: ``ends`` (points, mm) or ``open_ends``; ``thickness_mm``,
                ``placement`` (``inward``, ``outward``, ``internal``),
                ``inset_mm``, ``depth_mm``, ``contact_mm`` and ``name``.

        Returns:
            AdapterResult[dict[str, Any]]: ``feature``, ``caps``, ``ends``
            (each ``body`` and ``at_mm``), ``placement``, ``thickness_mm``,
            ``inset_mm`` and ``bodies_after``.
        """
        adapter = self._adapter(self)
        if wrong := _need_part(adapter, "create_end_caps"):
            return wrong
        placement = str(params.get("placement") or "inward")
        thickness = float(params.get("thickness_mm") or 5.0)
        inset = float(params.get("inset_mm") or 0.0)
        depth = float(params.get("depth_mm") or 0.0)

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            part = _sw_type_info.flagged(model, "IPartDoc")
            bodies = _joints.read_bodies(adapter, part, _byref_double)
            skipped: list[dict[str, Any]] = []
            coped: list[dict[str, Any]] = []
            if params.get("open_ends"):
                _, found = _joints.contacts(
                    adapter, bodies, float(params.get("contact_mm", 0.5)) / 1000.0
                )
                # A cap closes a free hollow end. An end half on what it meets
                # (a column top under a sloped beam) needs a bearing or cap
                # plate drawn to the joint, and an open section no cap at all.
                # A coped (curved) end takes no cap: SolidWorks refused one
                # and made none of the call's caps.
                ends = [
                    (e["body"], e["face"])
                    for e in found
                    if e["covered_percent"] == 0 and e["hollow"] and e["face"].planar
                ]
                skipped = [
                    {k: v for k, v in e.items() if k != "face"}
                    for e in found
                    if e["covered_percent"] > 0
                ]
                coped = [
                    {k: v for k, v in e.items() if k != "face"}
                    for e in found
                    if e["covered_percent"] == 0
                    and e["hollow"]
                    and not e["face"].planar
                ]
            else:
                faces, missed = _joints.end_faces_at(
                    adapter, bodies, [list(p) for p in params.get("ends") or []], 20.0
                )
                if missed:
                    raise InputRefused(
                        f"No member end within 20 mm of {missed}: give a point on or "
                        "near the end face of a structural member"
                    )
                owner = {id(f): b.name for b in bodies for f in b.ends}
                ends = [(owner.get(id(f), ""), f) for f in faces]
            if not ends:
                raise InputRefused(
                    "No open member end to cap: check_joints lists none"
                    if params.get("open_ends")
                    else "No member end found at the points given"
                )
            adapter._attempt(lambda: model.ClearSelection2(True))
            selected = 0
            for index, (_, face) in enumerate(ends):
                entity = _sw_type_info.flagged(face.ref, "IEntity")
                if adapter._attempt(
                    lambda e=entity, i=index: e.Select4(i > 0, _null_callout()),
                    default=False,
                ):
                    selected += 1
            if selected != len(ends):
                raise Exception(
                    f"SolidWorks selected {selected} of the {len(ends)} end faces to cap"
                )
            manager = _sw_type_info.flagged(model.FeatureManager, "IFeatureManager")
            feature = _feature(
                adapter._attempt(
                    lambda: manager.InsertEndCapFeature3(
                        thickness / 1000.0,
                        True,
                        False,
                        inset / 1000.0,
                        0.5,
                        0.0,
                        False,
                        depth / 1000.0,
                        False,
                        _joints.CAP_PLACEMENT[placement],
                    )
                )
            )
            if feature is None:
                raise Exception(
                    "SolidWorks refused the end caps: each end must be a planar end "
                    "face of a hollow or open structural member, and an inset must "
                    "leave the cap some size"
                )
            if params.get("name"):
                feature.Name = str(params["name"])
            described = _describe_caps(
                adapter, model, part, {b.name for b in bodies}, ends, thickness
            )
            after = len(_bodies(adapter, part))
            data: dict[str, Any] = {
                "feature": str(feature.Name),
                "caps": after - len(bodies),
                "descriptions": described,
                "ends": [
                    {
                        "body": name,
                        "at_mm": _joints.mm(
                            tuple(
                                sum(p[k] for p in _joints.samples_of(adapter, face))
                                / max(1, len(_joints.samples_of(adapter, face)))
                                for k in range(3)
                            )
                        ),
                    }
                    for name, face in ends
                ],
                "placement": placement,
                "thickness_mm": thickness,
                "inset_mm": inset,
                "bodies_after": after,
            }
            notes = []
            if data["caps"] != len(ends):
                notes.append(
                    f"{len(ends)} end(s) selected but {data['caps']} cap body(ies) made"
                )
            if skipped:
                data["not_capped"] = skipped[:_LISTED]
                notes.append(
                    f"{len(skipped)} end(s) partly on what they meet, not capped "
                    f"({', '.join(e['body'] for e in skipped[:5])}): give each a "
                    "bearing or cap plate under the member it meets (create_plates) "
                    "and trim it to the plate"
                )
            if coped:
                data["not_capped"] = (data.get("not_capped", []) + coped)[:_LISTED]
                notes.append(
                    f"{len(coped)} coped end(s) touching nothing, not capped "
                    f"({', '.join(e['body'] for e in coped[:5])}): a cap closes only "
                    "a flat end; trim each to what it should meet"
                )
            if notes:
                data["warning"] = "; ".join(notes)
            return data

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("create_end_caps", run),
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
    define: bool = True,
) -> tuple[str, list[Any], list[tuple[Point, Point]], dict[str, Any] | None]:
    """A new 3D sketch of one line per segment (mm), closed again.

    With ``define``, the ends that meet are merged and every node is
    dimensioned to the main planes before the sketch closes
    (:mod:`.sketch_define`), so the skeleton comes out fully defined.

    Returns:
        tuple: The sketch's name, its segments (raw dispatches, in order),
        each segment's ends in mm, and how it was defined (``None`` when
        ``define`` is off).
    """
    defined: dict[str, Any] | None = None
    model.ClearSelection2(True)
    _define.solve_on(adapter, manager)
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
        if define:
            open_sketch = _sw_type_info.flagged(manager.ActiveSketch, "ISketch")
            merged = _define.merge_nodes(model, open_sketch)
            defined = _define.tie_nodes_to_planes(adapter, model, manager, open_sketch)
            defined["merged_nodes"] = merged
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
    return str(feature.Name), raw, ends, defined


def _sketch_segments(
    adapter: Any, model: Any, sketch: str, names: list[str] | None
) -> tuple[str, list[Any], list[tuple[Point, Point]], bool]:
    """An existing sketch's lines and arcs (construction lines left out).

    Returns:
        tuple: The sketch's name, the chosen segments (raw dispatches),
        their ends in the sketch's own coordinates, and whether those are
        model coordinates (a 3D sketch).

    Raises:
        LookupError: No such sketch, or a named segment isn't in it.
    """
    feature = _feature(adapter._attempt(lambda: model.FeatureByName(sketch)))
    if feature is None:
        raise LookupError(f"No sketch named {sketch!r} in the part")
    found = _sw_type_info.flagged(feature.GetSpecificFeature2(), "ISketch")
    in_model_space = bool(adapter._attempt(lambda: found.Is3D(), default=False))
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
    return sketch, raw, ends, in_model_space


#: ``swCustomInfoType_e.swCustomInfoText``; ``swCustomPropertyReplaceValue``.
_TEXT_PROPERTY, _REPLACE_VALUE = 30, 2


def _describe_caps(
    adapter: Any,
    model: Any,
    part: Any,
    before: set[str],
    ends: list[tuple[str, Any]],
    thickness: float,
) -> list[str]:
    """Give the end caps' cut-list items a description: ``End cap PL5 90x90``.

    SolidWorks leaves an End Cap's cut-list item without one (seen live), so
    the shop's list showed a blank line per cap size. Each cap is measured
    across the end it closes (``IBody2::GetExtremePoint``); a cap whose
    volume is well under its square's is round.
    """
    caps = [
        b
        for b in _joints.read_bodies(adapter, part, _byref_double)
        if b.name not in before
    ]
    faces = [face for _, face in ends]
    labels: dict[str, str] = {}
    for cap in caps:
        mass = _mass(adapter, cap.ref)
        if mass is None or not faces:
            continue
        centre = mass[0]
        face = min(
            faces,
            key=lambda f: math.dist(
                centre, [(f.box[k] + f.box[k + 3]) / 2.0 for k in range(3)]
            ),
        )
        u, v = _joints._square_to(face.normal)
        a = _joints._span(adapter, cap, u) * 1000.0
        b = _joints._span(adapter, cap, v) * 1000.0
        big, small = max(a, b), min(a, b)
        round_cap = mass[1] * 1e9 < 0.85 * big * small * thickness
        size = f"D{big:.0f}" if round_cap else f"{big:.0f}x{small:.0f}"
        labels[cap.name] = f"End cap PL{thickness:g} {size}"
    for folder in _cut_list_folders(adapter, model, update=True):
        body_folder = _sw_type_info.flagged(folder.GetSpecificFeature2(), "IBodyFolder")
        names = [
            str(_sw_type_info.flagged(b, "IBody2").Name)
            for b in as_list(adapter._attempt(lambda f=body_folder: f.GetBodies()))
        ]
        label = next((labels[n] for n in names if n in labels), None)
        if label is None:
            continue
        properties = _sw_type_info.flagged(
            folder.CustomPropertyManager, "ICustomPropertyManager"
        )
        for key, value in (("Description", label), ("THICKNESS", f"{thickness:g}")):
            adapter._attempt(
                lambda k=key, v=value, m=properties: m.Add3(
                    k, _TEXT_PROPERTY, v, _REPLACE_VALUE
                )
            )
    return sorted(set(labels.values()))


#: A body whose box is this many times longer than its next side is a member.
_SLENDER = 2.5
#: How far inside a member's box (m) a node counts as standing in it.
_IN_MEMBER_M = 0.0005


def _member_nodes(
    adapter: Any, part: Any, ends: list[tuple[Point, Point]]
) -> set[Point]:
    """The segment ends (mm) that lie inside a member already in the part.

    A chain stops there: beams meeting at a column are trimmed to it, not
    mitred round it. Members are told by their boxes (a column's or a
    beam's is long and thin; a diagonal brace's isn't, and is left out).
    """
    boxes = []
    for raw in as_list(
        adapter._attempt(lambda: part.GetBodies2(0, True), default=None)
    ):
        box = as_list(
            adapter._attempt(
                lambda r=raw: _sw_type_info.flagged(r, "IBody2").GetBodyBox(),
                default=None,
            )
        )
        if len(box) != 6:
            continue
        sides = sorted(box[k + 3] - box[k] for k in range(3))
        if sides[2] >= _SLENDER * max(sides[1], 1e-9):
            boxes.append(box)
    found: set[Point] = set()
    for start, end in ends:
        for point in (start, end):
            p = [c / 1000.0 for c in point]
            if any(
                all(
                    b[k] - _IN_MEMBER_M <= p[k] <= b[k + 3] + _IN_MEMBER_M
                    for k in range(3)
                )
                for b in boxes
            ):
                found.add(point)
    return found


def _ends_of(segments: list[list[float]]) -> list[tuple[Point, Point]]:
    """Each six-number segment's two end points."""
    return [((s[0], s[1], s[2]), (s[3], s[4], s[5])) for s in segments]


#: The most member directions ``_Facing.report`` measures on the bodies.
_FACING_CHECKS = 12
#: A measured roll above this (degrees) means the facing rule didn't hold.
_ROLL_TOLERANCE_DEG = 0.5


class _Facing:
    """The ``Angle`` each group gets so its profile's height points ``up``, and its proof.

    ``up`` defaults to the model's y: beams, rafters and braces stand
    upright (a sloped rafter is no longer rolled by its slope), while a
    vertical member, which has no up of its own, keeps SolidWorks' facing
    unless it shares a chain with members that have one. ``rotation`` turns
    every group further. Only segments in model space (new or existing 3D
    sketches) and straight lines are turned; arcs and 2D sketches keep
    SolidWorks' facing plus ``rotation``.
    """

    def __init__(
        self,
        params: dict[str, Any],
        ends: list[tuple[Point, Point]],
        raw: list[Any],
        in_model_space: bool,
    ) -> None:
        self.rotation = math.radians(float(params.get("rotation") or 0.0))
        given = params.get("up")
        self.explicit = given is not None
        self.up = _orient.unit([float(v) for v in given]) if given else _orient.MODEL_UP
        self.ends = ends
        self.straight = [_is_line(segment) for segment in raw]
        self.active = in_model_space
        self.half_turn_same = _orient.symmetric(
            str(params.get("profile_type") or ""), str(params.get("size") or "")
        )
        self.turns: dict[tuple[int, ...], tuple[float | None, float]] = {}

    def _directions(self, group: list[int]) -> list[_orient.Vector]:
        return [
            _orient.direction(*end) for end in _orient.head_to_tail(self.ends, group)
        ]

    def angles(self, plan: list[list[int]]) -> list[float]:
        """Each group's ``Angle``, radians: the turn to ``up`` plus ``rotation``."""
        result = []
        for group in plan:
            turn: float | None = None
            spread = 0.0
            if self.active and all(self.straight[i] for i in group):
                turn, spread = _orient.group_turn(
                    self._directions(group), self.up, self.half_turn_same
                )
            self.turns[tuple(group)] = (turn, spread)
            result.append((turn or 0.0) + self.rotation)
        return result

    def report(
        self, adapter: Any, part: Any, feature: Any, plan: list[list[int]]
    ) -> dict[str, Any]:
        """How the groups were turned, measured on a body per member direction."""
        turned = [g for g in plan if self.turns.get(tuple(g), (None, 0))[0] is not None]
        kept = len(plan) - len(turned)
        spread = max((self.turns[tuple(g)][1] for g in turned), default=0.0)
        data: dict[str, Any] = {
            "up": [round(c, 4) for c in self.up],
            "up_given": self.explicit,
            "groups_turned": len(turned),
            "groups_kept": kept,
            "max_spread_deg": round(spread, 2),
        }
        rolls = self._measure(adapter, part, feature, turned)
        if rolls:
            data["checked_members"] = len(rolls)
            data["max_roll_deg"] = round(max(rolls), 2)
        notes = []
        if kept and self.active and self.explicit:
            notes.append(
                f"{kept} group(s) run along up and keep SolidWorks' facing: give "
                "those members their own call with an up square to them"
            )
        if spread > _orient.SAME_TURN_DEG:
            notes.append(
                f"a chain can't point every member's height at up (off by up to "
                f"{spread:.1f} degrees): split it, or give up in the chain's plane"
            )
        if rolls and max(rolls) > _ROLL_TOLERANCE_DEG:
            notes.append(
                f"{sum(r > _ROLL_TOLERANCE_DEG for r in rolls)} checked member(s) "
                f"came out turned up to {max(rolls):.1f} degrees off up; look with "
                "export_image and set rotation"
            )
        if notes:
            data["warning"] = "; ".join(notes)
        return data

    def _measure(
        self, adapter: Any, part: Any, feature: Any, groups: list[list[int]]
    ) -> list[float]:
        """Roll errors (degrees) of one body per distinct member direction."""
        names = set(_feature_bodies(adapter, feature))
        bodies = [
            b
            for b in _joints.read_bodies(adapter, part, _byref_double)
            if b.name in names
        ]
        seen: set[tuple[float, ...]] = set()
        rolls: list[float] = []
        for group in groups:
            if len(rolls) >= _FACING_CHECKS:
                break
            directions = self._directions(group)
            frames = _orient.chain_frames(directions)
            turn = self.turns[tuple(group)][0] or 0.0
            for (index, a), (_x, y) in zip(
                zip(group, directions, strict=True), frames, strict=True
            ):
                key = tuple(round(c, 3) for c in a) + (round(turn, 3),)
                if key in seen:
                    continue
                seen.add(key)
                start, end = self.ends[index]
                middle = tuple(
                    (s + e) / 2000.0 for s, e in zip(start, end, strict=True)
                )
                body = _member_at(adapter, bodies, middle, a)
                if body is None:
                    continue
                wanted = _orient.rotate(y, a, turn + self.rotation)
                roll = _orient.roll_error(
                    wanted, a, [f.normal for f in body.faces or [] if f.planar]
                )
                if roll is not None:
                    rolls.append(roll)
                break
        return rolls


def _is_line(segment: Any) -> bool:
    """Whether a sketch segment is a line (``swSketchSegments_e`` 0)."""
    wrapped = _sw_type_info.flagged(segment, "ISketchSegment")
    try:
        return int(wrapped.GetType()) == _SKETCH_LINE
    except Exception:  # an unreadable segment is treated as an arc: left as it is
        return False


def _member_at(
    adapter: Any, bodies: list[Any], point: tuple[float, ...], axis: Any
) -> Any:
    """The one member body whose box holds ``point`` (m) and which runs along ``axis``."""
    found = []
    for body in bodies:
        if not _joints.in_box(point, body.box, 0.001):
            continue
        _joints.read_faces(adapter, body)
        if body.axis is not None and abs(_orient.dot(body.axis, axis)) > 0.999:
            found.append(body)
    return found[0] if len(found) == 1 else None


def _insert_members(
    adapter: Any,
    features: Any,
    profile: Profile,
    raw: list[Any],
    plan: list[list[int]],
    params: dict[str, Any],
    angles: list[float],
) -> Any:
    """``InsertStructuralWeldment5`` with one group per chain; the feature or None.

    Args:
        angles: Each group's ``Angle``, radians (:class:`_Facing`).
    """
    treatment = CORNER_TREATMENTS[str(params.get("corner_treatment") or "miter")]
    mirror = str(params.get("mirror_profile") or "none")
    groups = []
    for chain, angle in zip(plan, angles, strict=True):
        group = _sw_type_info.flagged(
            features.CreateStructuralMemberGroup(), "IStructuralMemberGroup"
        )
        group.Segments = dispatch_array([raw[i] for i in chain])
        group.ApplyCornerTreatment = treatment != 0
        if treatment:
            group.CornerTreatmentType = treatment
        group.Angle = angle
        if mirror in MIRROR_AXES:
            group.MirrorProfile = True
            group.MirrorProfileAxis = MIRROR_AXES[mirror]
        group.MergeArcSegmentBodies = bool(params.get("merge_arcs"))
        groups.append(group)
    settle = float(os.getenv("SOLIDWORKS_MCP_COM_SETTLE_SECONDS", "0.25") or 0.0)
    for wait in (_REFUSED_RETRY_SETTLES * settle, None):
        inserted = adapter._attempt(
            lambda: features.InsertStructuralWeldment5(
                profile.path,
                _SIMPLE_CUT,
                True,
                dispatch_array(groups),
                profile.configuration,
            )
        )
        if inserted is not None or wait is None:
            return _feature(inserted)
        # Refused right after the same profile file was closed by the call
        # before (seen live with no pause between calls): asked again once.
        time.sleep(wait)
    return None


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
            centre, volume = mass
            # A trim changes a member's volume by its ends; a much smaller new
            # body is a piece cut off (a mitre tip), not the member.
            alike = {
                n: m for n, m in fresh.items() if m[1] >= _SAME_MEMBER_SHARE * volume
            }
            if alike:
                now = min(alike, key=lambda n: math.dist(centre, alike[n][0]))
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


#: A trimmed-off piece lying this close (m) inside a boundary body's box is an offcut.
_OFFCUT_M = 0.001


def _offcuts(
    adapter: Any, after: dict[str, Any], extra: list[str], boundary: list[Any]
) -> tuple[list[str], list[str]]:
    """The new bodies a trim left: leftovers to delete, and split pieces to report.

    Leftovers are copies of another body (same centre and volume: chained
    collinear beams trimmed to columns came back twice, seen live) and pieces
    lying inside a boundary member (a mitre tip inside a column). Any other
    new body is half of a member cut in two.
    """
    boxes = [
        as_list(adapter._attempt(lambda b=b: b.GetBodyBox(), default=None))
        for b in boundary
    ]
    boxes = [b for b in boxes if len(b) == 6]
    masses = {name: _mass(adapter, body) for name, body in after.items()}
    leftovers: list[str] = []
    split: list[str] = []
    for name in extra:
        mass = masses.get(name)
        twin = mass is not None and any(
            other != name
            and other not in leftovers
            and m is not None
            and math.dist(m[0], mass[0]) * 1000.0 <= _OFFCUT_M * 1000.0
            and abs(m[1] - mass[1]) <= 0.005 * max(m[1], 1e-12)
            for other, m in masses.items()
        )
        box = as_list(
            adapter._attempt(lambda n=name: after[n].GetBodyBox(), default=None)
        )
        inside = len(box) == 6 and any(
            all(
                box[k] >= o[k] - _OFFCUT_M and box[k + 3] <= o[k + 3] + _OFFCUT_M
                for k in range(3)
            )
            for o in boxes
        )
        (leftovers if twin or inside else split).append(name)
    return leftovers, split


def _delete_bodies(
    adapter: Any, model: Any, bodies: dict[str, Any], names: list[str], label: str
) -> str | None:
    """Delete bodies with a Delete/Keep Body feature named ``label``; its name, or None."""
    model.ClearSelection2(True)
    for index, name in enumerate(names):
        body = _sw_type_info.flagged(bodies[name], "IBody2")
        adapter._attempt(lambda b=body, i=index: b.Select2(i > 0, _null_callout()))
    manager = _sw_type_info.flagged(model.FeatureManager, "IFeatureManager")
    feature = _feature(
        adapter._attempt(lambda: manager.InsertDeleteBody2(False), default=None)
    )
    model.ClearSelection2(True)
    if feature is None:
        return None
    feature.Name = label
    return str(feature.Name)


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
