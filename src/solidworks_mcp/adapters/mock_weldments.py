"""The mock adapter's weldments: a small profile library, members and a cut list.

The library holds a few sizes named as SolidWorks 2025's own library names
them (DIN ``HE400B``, BSI ``RHS250x250x12.5``) so a search behaves as it does
live. Members are straight; each is one body as long as its segment, and
the cut list groups bodies of the same size and length, with the mass from
the size's kg per metre. A part with no material gets SolidWorks' default
density warning, as it does live.
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


def _find(table: dict[str, Any], name: str) -> str | None:
    """The key of ``table`` that ``name`` names, case and spacing aside."""
    return next((k for k in table if normalise(k) == normalise(name)), None)


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
        for s in segments:
            length = math.dist(s[:3], s[3:])
            state["bodies"].append((profile["size"], round(length, 3), per_metre))
        state["features"] += 1
        self._operation_count += 1
        name = (
            params.get("name")
            or f"{profile['type']} {profile['size']}({state['features']})"
        )
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
        for size, length, per_metre in bodies:
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
