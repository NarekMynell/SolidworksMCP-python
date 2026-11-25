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

import csv
import math
import os
import re
from typing import TYPE_CHECKING, Any

from .base import AdapterResult, AdapterResultStatus
from .solidworks.weldment_cuts import check_cut
from .solidworks.weldment_library import chains, normalise
from .solidworks.weldment_plates import check_plate, description, main_axis, plate_frame

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


def _tree_item(
    name: str, kind: str, defined: bool = True, absorbed: bool = False
) -> dict[str, Any]:
    """One feature of the mock tree."""
    return {
        "name": name,
        "type": kind,
        "folder": None,
        "defined": defined,
        "absorbed": absorbed,
    }


def _into_folder(
    tree: list[dict[str, Any]], folder: str, names: list[str]
) -> dict[str, Any]:
    """Put the named features of the mock tree in ``folder``, as the live call reports it."""
    by_name = {item["name"]: item for item in tree}
    moved, missing = [], []
    for name in names:
        if name in by_name:
            by_name[name]["folder"] = folder
            moved.append(name)
        else:
            missing.append(name)
    return {"folder": folder, "moved": moved, "missing": missing, "refused": []}


def _sketch_plane(
    frame: Any,
    label: str,
    planes: dict[tuple[int, float], str],
    tree: list[dict[str, Any]],
    made: list[str],
    prefix: str,
) -> tuple[str, bool]:
    """The plane a mock plate or cut is drawn on, and whether it was made for it alone.

    As live: a main plane at offset 0, a plane offset from it shared per call
    (``<prefix> Y+2745``), or a tilted plane of its own.
    """
    along = main_axis(frame.n)
    if along is None:
        plane = f"{label} plane"
        tree.append(_tree_item(plane, "RefPlane"))
        made.append(plane)
        return plane, True
    axis = along[0]
    offset = round(frame.origin[axis], 6)
    if abs(offset) < 1e-6:
        return ("Right Plane", "Top Plane", "Front Plane")[axis], False
    if (axis, offset) not in planes:
        planes[(axis, offset)] = f"{prefix} {'XYZ'[axis]}{offset:+g}"
        tree.append(_tree_item(planes[(axis, offset)], "RefPlane"))
        made.append(planes[(axis, offset)])
    return planes[(axis, offset)], False


def _area(points: list[tuple[float, float]]) -> float:
    """A polygon's signed area, mm²."""
    return (
        sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1], strict=True)
        )
        / 2.0
    )


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
        drawn = bool(params.get("segments"))
        if drawn:
            segments = [list(map(float, s)) for s in params["segments"]]
            sketch = str(
                params.get("sketch_name")
                or (f"{params['name']} skeleton" if params.get("name") else None)
                or f"3DSketch{len(state['sketches']) + 1}"
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
        defined = str(params.get("define") or "dimensions") == "dimensions"
        tree = state.setdefault("tree", [])
        if drawn:
            tree.append(_tree_item(sketch, "3DProfileFeature", defined=defined))
        tree.append(_tree_item(str(name), "WeldMemberFeat"))
        data: dict[str, Any] = {
            "feature": name,
            "profile": profile,
            "sketch": sketch,
            "segments": len(segments),
            "groups": groups,
            "grouping": grouping,
            "bodies_after": len(state["bodies"]),
        }
        if drawn:
            nodes = {tuple(s[:3]) for s in segments} | {tuple(s[3:]) for s in segments}
            data["skeleton"] = {
                "nodes": len(nodes),
                "dimensions": sum(1 for n in nodes for c in n if abs(c) > 1e-6)
                if defined
                else 0,
                "relations": sum(1 for n in nodes for c in n if abs(c) <= 1e-6)
                if defined
                else 0,
                "fixed": 0,
                "fixed_points": [],
                "definition": "fully defined" if defined else "under defined",
                "seconds": 0.0,
            }
        if params.get("folder"):
            data["folder"] = _into_folder(
                tree, str(params["folder"]), ([sketch] if drawn else []) + [str(name)]
            )
        return AdapterResult(status=AdapterResultStatus.SUCCESS, data=data)

    async def create_plates(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Mock plates: one body each, named, described, with its sketch and plane."""
        if error := self._part_error("create_plates"):
            return error
        state = self._weld_state()
        tree = state.setdefault("tree", [])
        defined = str(params.get("define") or "dimensions") == "dimensions"
        made: list[str] = []
        planes: dict[tuple[int, float], str] = {}
        tilted = 0
        results: list[dict[str, Any]] = []
        for spec in params["plates"]:
            try:
                points = check_plate(spec)
                frame = plate_frame(spec["origin"], spec["normal"], spec.get("x_axis"))
            except ValueError as error:
                return _error(str(error))
            label = (
                f"{spec['mark']} {spec['name']}"
                if spec.get("mark")
                else str(spec["name"])
            )
            plane, own = _sketch_plane(frame, label, planes, tree, made, "Plates")
            tilted += own
            tree.append(
                _tree_item(
                    f"{label} sketch", "ProfileFeature", defined=defined, absorbed=True
                )
            )
            tree.append(_tree_item(label, "Extrusion"))
            made.append(label)
            text = description(spec, points)
            state["bodies"].append((text, float(spec["thickness"]), 0.0, label, label))
            state.setdefault("plates", []).append(
                {
                    "label": label,
                    "mark": spec.get("mark") or "",
                    "description": text,
                    "thickness_mm": float(spec["thickness"]),
                    "area_mm2": abs(_area(points))
                    - sum(
                        math.pi * (h["diameter"] / 2) ** 2 * len(h["positions"])
                        for h in spec.get("holes") or []
                    ),
                }
            )
            results.append(
                {
                    "name": label,
                    "feature": label,
                    "sketch": f"{label} sketch",
                    "plane": plane,
                    "body": label,
                    "description": text,
                    "thickness_mm": float(spec["thickness"]),
                    "holes": sum(len(h["positions"]) for h in spec.get("holes") or []),
                    "definition": "fully defined" if defined else "not checked",
                }
            )
        self._operation_count += 1
        folder = params.get("folder")
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "plates": results,
                "planes_made": len(planes) + tilted,
                "folder": _into_folder(tree, str(folder), made) if folder else None,
                "bodies_after": len(state["bodies"]),
            },
        )

    async def create_cuts(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Mock cuts: a sketch and a cut each, on planes named as the real ones.

        Bodies aren't modelled as solids here, so which bodies a cut went
        through is reported as unknown (``None``), never guessed.
        """
        if error := self._part_error("create_cuts"):
            return error
        state = self._weld_state()
        tree = state.setdefault("tree", [])
        defined = str(params.get("define") or "dimensions") == "dimensions"
        made: list[str] = []
        planes: dict[tuple[int, float], str] = {}
        tilted = 0
        results: list[dict[str, Any]] = []
        for spec in params["cuts"]:
            try:
                check_cut(spec)
                frame = plate_frame(spec["origin"], spec["normal"], spec.get("x_axis"))
            except ValueError as error:
                return _error(str(error))
            label = str(spec["name"]).strip()
            plane, own = _sketch_plane(frame, label, planes, tree, made, "Cuts")
            tilted += own
            tree.append(
                _tree_item(
                    f"{label} sketch", "ProfileFeature", defined=defined, absorbed=True
                )
            )
            tree.append(_tree_item(label, "Cut"))
            made.append(label)
            results.append(
                {
                    "name": label,
                    "feature": label,
                    "sketch": f"{label} sketch",
                    "plane": plane,
                    "holes": sum(len(h["positions"]) for h in spec.get("holes") or []),
                    "bodies": None,
                    "definition": "fully defined" if defined else "not checked",
                }
            )
        self._operation_count += 1
        folder = params.get("folder")
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "cuts": results,
                "planes_made": len(planes) + tilted,
                "folder": _into_folder(tree, str(folder), made) if folder else None,
            },
        )

    async def pattern_members(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Mock patterns: each seed body copied, named as SolidWorks names copies."""
        if error := self._part_error("pattern_members"):
            return error
        state = self._weld_state()
        bodies = state["bodies"]
        seeds = []
        for name in params["members"]:
            found = [b for b in bodies if name in (b[3], b[4])] + [
                ("plate", None, 0.0, p["name"], p["name"])
                for p in state.get("plates", [])
                if p.get("name") == name
            ]
            if not found:
                return _error(f"No structural member or body named {name!r}")
            seeds.extend(found)
        label = str(params.get("name") or f"{params['members'][0]} pattern")
        if params.get("along"):
            kind = "linear"
            instances = int(params["along"]["count"]) * (
                int(params["along2"]["count"]) if params.get("along2") else 1
            )
            instances -= len(params.get("skip") or [])
            references = ["X axis"]
        elif params.get("around"):
            kind, instances, references = (
                "circular",
                int(params["around"]["count"]),
                [f"{label} axis"],
            )
        else:
            kind, instances, references = "mirror", 2, []
        for copy in range(1, instances):
            for size, length, kg, _feature, _body in seeds:
                bodies.append((size, length, kg, label, f"{label}[{copy}]"))
        tree = state.setdefault("tree", [])
        tree.extend(_tree_item(r, "RefAxis") for r in references)
        type_name = {"linear": "LPattern", "circular": "CirPattern"}.get(
            kind, "MirrorSolid"
        )
        tree.append(_tree_item(label, type_name))
        self._operation_count += 1
        folder = params.get("folder")
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "feature": label,
                "kind": kind,
                "instances": instances,
                "seed_bodies": len(seeds),
                "bodies_added": len(seeds) * (instances - 1),
                "bodies_after": len(bodies),
                "references": references,
                "folder": _into_folder(tree, str(folder), [*references, label])
                if folder
                else None,
            },
        )

    async def create_vent_holes(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Mock vents: a vent and a drain in each hollow body named (HxBxT sizes)."""
        if error := self._part_error("create_vent_holes"):
            return error
        state = self._weld_state()
        lines = state.get("lines", {})
        by_feature: dict[str, list[tuple[str, str]]] = {}
        for size, _length, _kg, feature, body in state["bodies"]:
            by_feature.setdefault(feature, []).append((body, size))
        holes: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for name in params["members"]:
            targets = by_feature.get(name) or [
                (body, size)
                for f in by_feature.values()
                for body, size in f
                if body == name
            ]
            if not targets:
                return _error(f"No structural member or body named {name!r}")
            for body, size in targets:
                if not re.search(r"\d\s*x\s*\d+(\.\d+)?\s*x", str(size), re.IGNORECASE):
                    skipped.append(
                        {
                            "member": body,
                            "why": "no flat walls (a round tube, or not a member)",
                        }
                    )
                    continue
                line = lines.get(body, [0, 0, 0, 0, 0, 0])
                for kind, at in (("vent", line[3:]), ("drain", line[:3])):
                    holes.append(
                        {
                            "member": body,
                            "kind": kind,
                            "feature": f"{body} {kind}",
                            "at_mm": list(at),
                        }
                    )
        self._operation_count += 1
        folder = params.get("folder")
        tree = state.setdefault("tree", [])
        for hole in holes:
            tree.append(_tree_item(hole["feature"], "Cut", absorbed=False))
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "holes": holes,
                "skipped": skipped,
                "folder": _into_folder(tree, str(folder), [h["feature"] for h in holes])
                if folder and holes
                else None,
            },
        )

    async def export_plate_dxfs(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Mock plate DXFs: one small DXF per distinct plate, and plates.csv."""
        if error := self._part_error("export_plate_dxfs"):
            return error
        path = str(getattr(self._current_model, "path", "") or "")
        if not path or path.startswith("Mock://"):
            return _error(
                "The part has never been saved; save it with save_file, then export its plates."
            )
        out_dir = str(params["output_dir"])
        prefix = str(params.get("prefix") or "")
        os.makedirs(out_dir, exist_ok=True)
        # Like SolidWorks' cut list: identical plates are one item; holes make
        # two plates of one size different, so the area is in the key.
        groups: dict[tuple[str, float, float], list[dict[str, Any]]] = {}
        for plate in self._weld_state().get("plates", []):
            key = (
                plate["description"],
                plate["thickness_mm"],
                round(plate["area_mm2"], 1),
            )
            groups.setdefault(key, []).append(plate)
        files = []
        for (text, thickness, _area_mm2), plates in groups.items():
            mark = plates[0]["mark"]
            stem = re.sub(
                r"[^A-Za-z0-9._-]+",
                "_",
                f"{prefix}{mark or text}_t{thickness:g}_x{len(plates)}",
            ).strip("_")
            file = os.path.join(out_dir, f"{stem}.dxf")
            with open(file, "w", encoding="ascii") as handle:
                handle.write("0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n")
            files.append(
                {
                    "file": file,
                    "mark": mark,
                    "description": text,
                    "thickness_mm": thickness,
                    "quantity": len(plates),
                    "area_mm2": round(plates[0]["area_mm2"], 1),
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
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={"files": files, "csv": listing, "skipped": []},
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
