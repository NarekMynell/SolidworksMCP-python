"""Feature-tree tools for the mock adapter: folders, names and the audit.

The mock tree holds what the mock weldment tools made (skeleton sketches,
members, plate planes, sketches and extrusions), each with its folder and
whether its sketch is fully defined.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from .base import AdapterResult, AdapterResultStatus
from .solidworks.feature_tree import FOLDER_THRESHOLD, GENERIC_NAME


class MockFeatureTreeMixin:
    """Organize and audit the mock tree."""

    _current_model: Any

    if TYPE_CHECKING:

        def _weld_state(self) -> dict[str, Any]:  # provided by MockWeldmentsMixin
            return {}

    def _tree(self) -> list[dict[str, Any]] | None:
        if self._current_model is None:
            return None
        return cast(list[dict[str, Any]], self._weld_state().setdefault("tree", []))

    async def set_feature_folders(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Mock renames and folders."""
        tree = self._tree()
        if tree is None:
            return AdapterResult(
                status=AdapterResultStatus.ERROR, error="No active model"
            )
        by_name = {item["name"]: item for item in tree}
        renamed: dict[str, str] = {}
        missing: list[str] = []
        for old, new in (params.get("rename") or {}).items():
            if old not in by_name:
                missing.append(old)
                continue
            by_name[old]["name"] = str(new)
            renamed[old] = str(new)
        by_name = {item["name"]: item for item in tree}
        folders = []
        for spec in params.get("folders") or []:
            moved, absent = [], []
            for name in spec.get("features") or []:
                if name in by_name:
                    by_name[name]["folder"] = spec["name"]
                    moved.append(name)
                else:
                    absent.append(name)
            folders.append(
                {
                    "folder": spec["name"],
                    "moved": moved,
                    "missing": absent,
                    "refused": [],
                }
            )
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "renamed": renamed,
                "unknown_names": missing,
                "names_refused": [],
                "folders": folders,
            },
        )

    async def check_feature_tree(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Mock audit of the mock tree."""
        tree = self._tree()
        if tree is None:
            return AdapterResult(
                status=AdapterResultStatus.ERROR, error="No active model"
            )
        limit = int(params.get("max_listed") or 20)
        by_type: dict[str, int] = {}
        for item in tree:
            by_type[item["type"]] = by_type.get(item["type"], 0) + 1
        generic = [i["name"] for i in tree if GENERIC_NAME.match(i["name"])]
        sketches = [
            i for i in tree if i["type"] in ("ProfileFeature", "3DProfileFeature")
        ]
        loose = [i["name"] for i in sketches if not i["defined"]]
        outside = [i["name"] for i in tree if not i["folder"] and not i["absorbed"]]
        folders: dict[str, int] = {}
        for item in tree:
            if item["folder"]:
                folders[item["folder"]] = folders.get(item["folder"], 0) + 1
        issues = []
        if loose:
            issues.append(
                f"{len(loose)} sketch(es) not fully defined: they move when "
                "dragged; tie them to the origin and planes"
            )
        if generic:
            issues.append(
                f"{len(generic)} feature(s) keep SolidWorks' own names "
                "(Sketch12, Plane7...): name them for what they are"
            )
        if len(outside) > FOLDER_THRESHOLD:
            issues.append(
                f"{len(outside)} features outside folders: group them "
                "(skeleton, members, trims, plates, holes)"
            )
        states = {
            state: count
            for state, count in (
                ("fully defined", len(sketches) - len(loose)),
                ("under defined", len(loose)),
            )
            if count
        }
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "features": len(tree),
                "by_type": by_type,
                "folders": [{"name": n, "features": c} for n, c in folders.items()],
                "outside_folders": len(outside),
                "generic_names": generic[:limit],
                "generic_count": len(generic),
                "sketches": states,
                "not_fully_defined": loose[:limit],
                "suppressed": [],
                "issues": issues,
                "verdict": "clean" if not issues else "needs work",
            },
        )
