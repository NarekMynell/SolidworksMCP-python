"""The mock adapter's weldments: a small profile library, members and a cut list.

The library holds a few sizes named as SolidWorks 2025's own library names
them (DIN ``HE400B``, BSI ``RHS250x250x12.5``) so a search behaves as it does
live. Members are straight; each is one body as long as its segment, and
the cut list groups bodies of the same size and length, with the mass from
the size's kg per metre. A part with no material gets SolidWorks' default
density warning, as it does live.

The joint check works on the centre lines: members that meet overlap until
the one that ends on the other is trimmed (or, at a corner of one chain,
mitred); an end that meets nothing and isn't capped is open.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

from .base import AdapterResult, AdapterResultStatus
from .solidworks.weldment_library import chains, normalise

#: ``{standard: {type: {size: kg per metre}}}``, nominal masses.
SAMPLE_PROFILES: dict[str, dict[str, dict[str, float]]] = {
    "DIN": {
        "ipe": {"IPE100": 8.1, "IPE200": 22.4, "IPE300": 42.2},
        "hea": {"HE300A": 88.3, "HE300B": 117.0, "HE400B": 155.0},
    },
    "BSI": {
        "shs": {"RHS60x60x4": 6.97, "RHS100x100x5": 14.7, "RHS250x250x12.5": 92.6},
    },
    "iso": {"square tube": {"20 x 20 x 2": 1.12, "80 x 80 x 5": 11.4}},
}

_STEEL = re.compile(r"steel|s235|s275|s355", re.IGNORECASE)


def _error(message: str) -> AdapterResult[dict[str, Any]]:
    return AdapterResult(status=AdapterResultStatus.ERROR, error=message)


def _distance_to_line(point: list[float], line: list[float]) -> float:
    """How far a point is from a segment ``[x1, y1, z1, x2, y2, z2]``, mm."""
    a, b = line[:3], line[3:]
    ab = [b[k] - a[k] for k in range(3)]
    length2 = sum(v * v for v in ab)
    t = 0.0
    if length2 > 0:
        t = max(
            0.0, min(1.0, sum((point[k] - a[k]) * ab[k] for k in range(3)) / length2)
        )
    return math.dist(point, [a[k] + t * ab[k] for k in range(3)])


#: Centre lines this close meet (mm).
_MEETS_MM = 1.0


def _find(table: dict[str, Any], name: str) -> str | None:
    """The key of ``table`` that ``name`` names, case and spacing aside."""
    return next((k for k in table if normalise(k) == normalise(name)), None)


def _groups(names: list[str], links: set[tuple[str, str]]) -> list[list[str]]:
    """Connected groups of names, largest first."""
    found: list[set[str]] = []
    for name in names:
        found.append({name})
    for a, b in links:
        first = next(g for g in found if a in g)
        second = next(g for g in found if b in g)
        if first is not second:
            first |= second
            found.remove(second)
    return sorted((sorted(g) for g in found), key=len, reverse=True)


class MockWeldmentsMixin:
    """Weldment tools for the mock adapter."""

    _current_model: Any
    _operation_count: int

    if TYPE_CHECKING:

        def _mock_doc_key(self) -> str:  # provided by MockSolidWorksAdapter
            return ""

    def _weld_state(self) -> dict[str, Any]:
        """This document's sketches and member bodies."""
        if not hasattr(self, "_weldments"):
            self._weldments: dict[str, dict[str, Any]] = {}
        return self._weldments.setdefault(
            self._mock_doc_key(), {"sketches": {}, "bodies": [], "features": 0}
        )

    def _part_error(self, operation: str) -> AdapterResult[dict[str, Any]] | None:
        model = self._current_model
        if model is None:
            return _error("No active model")
        if model.type != "Part":
            return _error(
                f"{operation} needs an open part (structural members live in a "
                "weldment part; create_part makes one)"
            )
        return None

    async def list_weldment_profiles(
        self,
        standard: str | None = None,
        profile_type: str | None = None,
        query: str | None = None,
        limit: int = 50,
    ) -> AdapterResult[dict[str, Any]]:
        """Mock listing or searching the sample library."""
        base: dict[str, Any] = {"roots": ["<mock library>"]}
        if query:
            words = [normalise(w) for w in query.split() if w.strip()]
            matches = [
                {
                    "standard": std,
                    "type": kind,
                    "size": size,
                    "path": f"{std}/{kind}.sldlfp",
                }
                for std, kinds in SAMPLE_PROFILES.items()
                if not standard or normalise(std) == normalise(standard)
                for kind, sizes in kinds.items()
                for size in sizes
                if all(w in normalise(kind) + "|" + normalise(size) for w in words)
            ]
            return AdapterResult(
                status=AdapterResultStatus.SUCCESS,
                data={
                    **base,
                    "query": query,
                    "matches": matches[:limit],
                    "truncated": len(matches) > limit,
                },
            )
        if standard is None:
            return AdapterResult(
                status=AdapterResultStatus.SUCCESS,
                data={
                    **base,
                    "standards": [
                        {"standard": s, "types": sorted(t)}
                        for s, t in SAMPLE_PROFILES.items()
                    ],
                },
            )
        std = _find(SAMPLE_PROFILES, standard)
        if std is None:
            return _error(
                f"No weldment profile standard {standard!r}; the library has "
                f"{', '.join(SAMPLE_PROFILES)}"
            )
        kinds = SAMPLE_PROFILES[std]
        if profile_type is None:
            types = [{"type": k, "sizes": list(v)} for k, v in kinds.items()]
            return AdapterResult(
                status=AdapterResultStatus.SUCCESS,
                data={**base, "standard": std, "types": types},
            )
        kind = _find(kinds, profile_type)
        if kind is None:
            return _error(
                f"No profile type {profile_type!r} in {std}; it has {', '.join(kinds)}."
            )
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={**base, "standard": std, "type": kind, "sizes": list(kinds[kind])},
        )

    def _profile(self, params: dict[str, Any]) -> tuple[dict[str, str], float] | str:
        """The profile and its kg per metre, or an error message."""
        size = str(params.get("size") or "")
        if params.get("profile_path"):
            path = str(params["profile_path"])
            return {
                "standard": "custom",
                "type": path,
                "size": size,
                "path": path,
            }, 10.0
        std = _find(SAMPLE_PROFILES, str(params.get("standard") or ""))
        if std is None:
            return f"No weldment profile standard {params.get('standard')!r}"
        kind = _find(SAMPLE_PROFILES[std], str(params.get("profile_type") or ""))
        if kind is None:
            return f"No profile type {params.get('profile_type')!r} in {std}"
        sizes = SAMPLE_PROFILES[std][kind]
        found = _find(sizes, size)
        if found is None:
            return (
                f"No size {size!r} for {std} {kind}; it has {len(sizes)} sizes. "
                "list_weldment_profiles lists them all."
            )
        path = f"{std}/{kind}.sldlfp"
        return {"standard": std, "type": kind, "size": found, "path": path}, sizes[
            found
        ]

    async def create_structural_members(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Mock members: one straight body per segment, grouped as live."""
        if error := self._part_error("create_structural_members"):
            return error
        resolved = self._profile(params)
        if isinstance(resolved, str):
            return _error(resolved)
        profile, per_metre = resolved
        state = self._weld_state()
        if params.get("segments"):
            segments = [list(map(float, s)) for s in params["segments"]]
            sketch = str(
                params.get("sketch_name") or f"3DSketch{len(state['sketches']) + 1}"
            )
            state["sketches"][sketch] = segments
        else:
            sketch = str(params.get("sketch") or "")
            if sketch not in state["sketches"]:
                return _error(f"No sketch named {sketch!r} in the part")
            segments = state["sketches"][sketch]
        ends = [((s[0], s[1], s[2]), (s[3], s[4], s[5])) for s in segments]
        grouping = str(params.get("grouping") or "auto")
        groups = {
            "single": 1,
            "each": len(segments),
            "auto": len(chains(ends)),
        }[grouping]
        state["features"] += 1
        self._operation_count += 1
        name = (
            params.get("name")
            or f"{profile['type']} {profile['size']}({state['features']})"
        )
        chained = (
            str(params.get("corner_treatment") or "miter") != "none"
            and grouping != "each"
        )
        for index, s in enumerate(segments, 1):
            length = math.dist(s[:3], s[3:])
            body = f"{name}[{index}]"
            state["bodies"].append(
                (profile["size"], round(length, 3), per_metre, name, body)
            )
            state.setdefault("lines", {})[body] = list(s)
            if chained:
                state.setdefault("mitred", set()).add(body)
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "feature": name,
                "profile": profile,
                "sketch": sketch,
                "segments": len(segments),
                "groups": groups,
                "grouping": grouping,
                "bodies_after": len(state["bodies"]),
            },
        )

    def _trim_bodies(self, names: list[str]) -> list[int] | str:
        """Indexes of the bodies ``names`` name (bodies or members), or an error."""
        bodies = self._weld_state()["bodies"]
        chosen: list[int] = []
        for name in names:
            hits = [i for i, b in enumerate(bodies) if name in (b[3], b[4])]
            if not hits:
                members = ", ".join(dict.fromkeys(b[3] for b in bodies)) or "none"
                return (
                    f"No structural member or body named {name!r}; the part has "
                    f"the members {members}"
                )
            chosen.extend(i for i in hits if i not in chosen)
        return chosen

    async def create_trim_extend(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Mock Trim/Extend: each member is cut a nominal ``max(gap, 1)`` mm short."""
        if error := self._part_error("create_trim_extend"):
            return error
        members = self._trim_bodies(list(params.get("members") or []))
        if isinstance(members, str):
            return _error(members)
        boundary = self._trim_bodies(list(params.get("boundary") or []))
        if isinstance(boundary, str):
            return _error(boundary)
        bodies = self._weld_state()["bodies"]
        both = sorted(bodies[i][4] for i in set(members) & set(boundary))
        if both:
            return _error(f"{', '.join(both)} can't be both trimmed and the boundary")
        corner = str(params.get("corner_type") or "end_trim")
        if corner != "end_trim" and (len(members), len(boundary)) != (1, 1):
            return _error(
                f"A {corner} corner joins two members: give one member body and "
                f"one boundary body (got {len(members)} and {len(boundary)})"
            )
        gap = float(params.get("weld_gap_mm") or 0.0)
        cut_mm = max(gap, 1.0)
        state = self._weld_state()
        state["trims"] = state.get("trims", 0) + 1
        self._operation_count += 1
        feature_name = params.get("name") or f"Trim/Extend{state['trims']}"
        changes = []
        for count, i in enumerate(members, 1):
            size, length, per_metre, member, body = bodies[i]
            now = f"{feature_name}[{count}]"  # SolidWorks renames trimmed bodies
            bodies[i] = (size, round(length - cut_mm, 3), per_metre, member, now)
            lines = state.setdefault("lines", {})
            if body in lines:
                lines[now] = lines.pop(body)
            state.setdefault("trimmed", set()).add(now)
            area_mm2 = per_metre / 7850.0 * 1e6
            changes.append(
                {
                    "name": body,
                    "now": now,
                    "volume_change_mm3": round(-area_mm2 * cut_mm, 3),
                    "changed": True,
                }
            )
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "feature": feature_name,
                "corner_type": corner,
                "cut": str(params.get("cut") or "simple"),
                "weld_gap_mm": gap,
                "members": changes,
                "boundary": [bodies[i][4] for i in boundary],
                "unchanged": [],
                "bodies_after": len(bodies),
            },
        )

    def _joint_survey(self) -> dict[str, Any]:
        """Overlapping pairs, open ends and links of the mock members' centre lines."""
        state = self._weld_state()
        lines: dict[str, list[float]] = state.get("lines", {})
        trimmed: set[str] = state.get("trimmed", set())
        mitred: set[str] = state.get("mitred", set())
        caps: list[list[float]] = state.get("caps", [])
        names = [b[4] for b in state["bodies"] if b[4] in lines]
        overlaps, links, open_ends = [], set(), []
        for name in names:
            line = lines[name]
            for end in (line[:3], line[3:]):
                met = [
                    other
                    for other in names
                    if other != name
                    and _distance_to_line(end, lines[other]) <= _MEETS_MM
                ]
                for other in met:
                    links.add(tuple(sorted((name, other))))
                    other_line = lines[other]
                    corner = (
                        min(
                            math.dist(end, other_line[:3]),
                            math.dist(end, other_line[3:]),
                        )
                        <= _MEETS_MM
                    )
                    clean = name in trimmed or (
                        corner and (other in trimmed or {name, other} <= mitred)
                    )
                    if not clean:
                        overlaps.append(tuple(sorted((name, other))))
                capped = any(math.dist(end, c) <= _MEETS_MM for c in caps)
                if not met and not capped:
                    open_ends.append(
                        {"body": name, "at_mm": [round(c, 1) for c in end]}
                    )
        return {
            "names": names,
            "overlaps": sorted(set(overlaps)),
            "links": links,
            "open_ends": open_ends,
        }

    async def check_joints(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Mock joint check on the members' centre lines."""
        if error := self._part_error("check_joints"):
            return error
        state = self._weld_state()
        if not state["bodies"]:
            return _error("The part has no bodies to check")
        survey = self._joint_survey()
        listed = int(params.get("max_listed", 25))
        only = None
        if params.get("members"):
            chosen = self._trim_bodies(list(params["members"]))
            if isinstance(chosen, str):
                return _error(chosen)
            only = {state["bodies"][i][4] for i in chosen}
        pairs = [p for p in survey["overlaps"] if only is None or set(p) & only]
        ends = [e for e in survey["open_ends"] if only is None or e["body"] in only]
        linked = {n for link in survey["links"] for n in link}
        loose = [
            n
            for n in survey["names"]
            if n not in linked and (only is None or n in only)
        ]
        if len(survey["names"]) < 2:
            loose = []
        groups = _groups(survey["names"], survey["links"])
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "bodies": len(state["bodies"]),
                "checked_bodies": len(state["bodies"]) if only is None else len(only),
                "members": len(survey["names"]),
                "overlaps": {
                    "count": len(pairs),
                    "volume_mm3": round(1000.0 * len(pairs), 1),
                    "pairs": [
                        {
                            "bodies": list(p),
                            "volume_mm3": 1000.0,
                            "at_mm": [0.0, 0.0, 0.0],
                        }
                        for p in pairs[:listed]
                    ],
                },
                "open_ends": {"count": len(ends), "ends": ends[:listed]},
                "partly_open_ends": {"count": 0, "ends": []},
                "loose": loose[:listed],
                "loose_count": len(loose),
                "groups": len(groups),
                "group_sizes": [len(g) for g in groups[:listed]],
                "clean": not pairs and not loose,
                "pairs_checked": len(pairs),
                "seconds": 0.0,
            },
        )

    async def create_end_caps(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Mock end caps: a plate body at each open end, or at the ends nearest the points."""
        if error := self._part_error("create_end_caps"):
            return error
        state = self._weld_state()
        survey = self._joint_survey()
        if params.get("open_ends"):
            ends = survey["open_ends"]
        else:
            lines = state.get("lines", {})
            all_ends = [
                {"body": n, "at_mm": [round(c, 1) for c in end]}
                for n in survey["names"]
                for end in (lines[n][:3], lines[n][3:])
            ]
            ends, missed = [], []
            for point in params.get("ends") or []:
                near = [e for e in all_ends if math.dist(point, e["at_mm"]) <= 20.0]
                if not near:
                    missed.append(list(point))
                    continue
                best = min(near, key=lambda e: math.dist(point, e["at_mm"]))
                if best not in ends:
                    ends.append(best)
            if missed:
                return _error(
                    f"No member end within 20 mm of {missed}: give a point on or near "
                    "the end face of a structural member"
                )
        if not ends:
            return _error("No open member end to cap: check_joints lists none")
        state["end_caps"] = state.get("end_caps", 0) + 1
        self._operation_count += 1
        feature = params.get("name") or f"End cap{state['end_caps']}"
        thickness = float(params.get("thickness_mm") or 5.0)
        for index, end in enumerate(ends, 1):
            state.setdefault("caps", []).append(end["at_mm"])
            state["bodies"].append(
                ("End cap", thickness, 0.0, feature, f"{feature}[{index}]")
            )
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "feature": feature,
                "caps": len(ends),
                "ends": ends,
                "placement": str(params.get("placement") or "inward"),
                "thickness_mm": thickness,
                "inset_mm": float(params.get("inset_mm") or 0.0),
                "bodies_after": len(state["bodies"]),
            },
        )

    async def get_cut_list(
        self, update: bool = True, export_path: str | None = None
    ) -> AdapterResult[dict[str, Any]]:
        """Mock cut list: bodies of one size and length are one item."""
        if error := self._part_error("get_cut_list"):
            return error
        if export_path and not export_path.lower().endswith(".csv"):
            return _error("export_path must end in .csv")
        bodies = self._weld_state()["bodies"]
        if not bodies:
            return _error(
                "The part has no cut list: it is not a weldment. "
                "create_structural_members makes one."
            )
        material = getattr(self, "_materials", {}).get(self._mock_doc_key(), {})
        name = material.get("name", "Material <not specified>")
        steel = material.get("classification") == "Steel" or _STEEL.search(name)
        density_factor = 1.0 if steel else 1000.0 / 7850.0
        groups: dict[tuple[str, float], list[float]] = {}
        for size, length, per_metre, *_names in bodies:
            groups.setdefault((size, length), []).append(per_metre)
        items = []
        for index, ((size, length), masses) in enumerate(groups.items(), 1):
            each = round(masses[0] * length / 1000.0 * density_factor, 3)
            items.append(
                {
                    "item": index,
                    "name": f"{size}<{index}>",
                    "description": size,
                    "quantity": len(masses),
                    "length_mm": length,
                    "angle1_deg": 0.0,
                    "angle2_deg": 0.0,
                    "total_length_mm": round(length * len(masses), 3),
                    "profile_total_length_mm": round(
                        sum(b[1] for b in bodies if b[0] == size), 3
                    ),
                    "material": name,
                    "mass_each_kg": each,
                    "mass_total_kg": round(each * len(masses), 3),
                }
            )
        data: dict[str, Any] = {
            "items": items,
            "item_count": len(items),
            "pieces": len(bodies),
            "total_length_mm": round(sum(i["total_length_mm"] for i in items), 3),
            "total_mass_kg": round(sum(i["mass_total_kg"] for i in items), 3),
        }
        if "not specified" in name:
            data["warning"] = (
                "Some items have no material, so their mass uses SolidWorks' "
                "default density; set_material assigns one."
            )
        if export_path:
            data["export_path"] = export_path
        return AdapterResult(status=AdapterResultStatus.SUCCESS, data=data)
