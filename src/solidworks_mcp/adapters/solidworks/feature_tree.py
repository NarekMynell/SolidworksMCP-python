"""The feature tree as a CAD specialist reads it: folders, names, defined sketches.

A model built by many tool calls ends up as a long flat list of ``Sketch12``,
``Plane7`` and ``Boss-Extrude31``, every sketch under defined: hard to find
anything in, and anything dragged moves. This mixin groups features into
folders, renames them, and audits the tree for what a specialist looks at
first.

Checked live on SolidWorks 2025 SP4 before it was written:

- ``IFeatureManager::InsertFeatureTreeFolder2(swFeatureTreeFolder_Containing)``
  puts the selected features in a new folder (type ``FtrFolder``), which takes
  its name like any feature.
- ``ISketch::GetConstrainedStatus`` gives a sketch's definition
  (``swConstrainedStatus_e``); every sketch of two scarecrow weldments built by
  agents came back under defined (2), the complaint this answers.
"""

from __future__ import annotations

import re
from typing import Any, cast

from .. import sw_type_info as _sw_type_info
from ..base import AdapterResult, AdapterResultStatus
from .com_utils import as_list
from .sketch_define import DEFINITION

#: ``swFeatureTreeFolderType_e.swFeatureTreeFolder_Containing``.
_FOLDER_CONTAINING = 2
#: ``swMoveLocation_e.swMoveToFolder`` for ``IModelDocExtension::ReorderFeature``.
_MOVE_TO_FOLDER = 5
#: ``swMoveLocation_e.swMoveAfter``.
_MOVE_AFTER = 3
#: Feature types the tree always has, which nobody organizes.
SYSTEM_TYPES = frozenset(
    {
        "CommentsFolder",
        "FavoriteFolder",
        "HistoryFolder",
        "SelectionSetFolder",
        "SensorFolder",
        "DocsFolder",
        "DetailCabinet",
        "SurfaceBodyFolder",
        "SolidBodyFolder",
        "CutListFolder",
        "SubWeldFolder",
        "EnvFolder",
        "InkMarkupFolder",
        "EqnFolder",
        "MaterialFolder",
        "OriginProfileFeature",
        "MateGroup",
        "BlockFolder",
        "LiveSectionFolder",
        "MBDViewFolder",
        "GridFeature",
    }
)
#: Names SolidWorks gives by itself (English templates): a word and a number.
GENERIC_NAME = re.compile(
    r"^(Sketch|3DSketch|Plane|Axis|Point|Coordinate System|Boss-Extrude|"
    r"Cut-Extrude|Extrude-Thin|Revolve|Cut-Revolve|Sweep|Cut-Sweep|Loft|"
    r"Cut-Loft|Fillet|Chamfer|Shell|Rib|Draft|Mirror|LPattern|CirPattern|"
    r"Structural Member|Trim/Extend|End cap|Gusset|Fillet Bead|Combine|"
    r"Split|Delete/Keep Body|Body-Move/Copy|Hole|Folder)\d+$"
)
#: Sketch feature types.
_SKETCH_TYPES = ("ProfileFeature", "3DProfileFeature")
#: A part with more modelling features than this outside folders needs them.
FOLDER_THRESHOLD = 12


def _flagged(obj: Any, interface: str) -> Any:
    """``obj`` with ``interface``'s methods flagged for late binding."""
    return _sw_type_info.flagged(obj, interface)


def top_features(model: Any) -> list[Any]:
    """The model's top-level features in tree order, flagged as ``IFeature``.

    Args:
        model: The ``IModelDoc2``.

    Returns:
        list[Any]: The features, folders and the features in them alike.
    """
    found: list[Any] = []
    feature = model.FirstFeature()
    while feature is not None:
        feature = _flagged(feature, "IFeature")
        found.append(feature)
        feature = feature.GetNextFeature()
    return found


def folders_of(features: list[Any]) -> dict[str, list[str]]:
    """Each feature folder's name and the names of the features in it.

    Args:
        features: Top-level features from :func:`top_features`.

    Returns:
        dict[str, list[str]]: Folder name to feature names, tree order.
    """
    folders: dict[str, list[str]] = {}
    for feature in features:
        if feature.GetTypeName2() != "FtrFolder":
            continue
        name = str(feature.Name)
        if name.endswith("___EndTag___"):
            continue
        folder = _flagged(feature.GetSpecificFeature2(), "IFeatureFolder")
        folders[name] = [
            str(_flagged(inner, "IFeature").Name)
            for inner in as_list(folder.GetFeatures())
        ]
    return folders


def move_into_folder(
    adapter: Any, model: Any, folder: str, names: list[str]
) -> dict[str, Any]:
    """Put features in a folder, making the folder when the tree has none of that name.

    Args:
        adapter: The connected adapter.
        model: The part's ``IModelDoc2``.
        folder: The folder's name.
        names: Feature names, tree order.

    Returns:
        dict[str, Any]: ``folder``, ``moved`` (names now in it), ``missing``
        (no such feature) and ``refused`` (SolidWorks wouldn't move them).
    """
    features = {str(f.Name): f for f in top_features(model)}
    missing = [name for name in names if name not in features]
    wanted = [name for name in names if name in features]
    existing = folders_of(list(features.values()))
    moved: list[str] = []
    refused: list[str] = []
    manager = _flagged(model.FeatureManager, "IFeatureManager")
    if folder not in existing and wanted:
        model.ClearSelection2(True)
        selected = [
            name for i, name in enumerate(wanted) if features[name].Select2(i > 0, 0)
        ]
        made = adapter._attempt(
            lambda: manager.InsertFeatureTreeFolder2(_FOLDER_CONTAINING), default=None
        )
        model.ClearSelection2(True)
        if made is None:
            refused = list(wanted)
        else:
            _flagged(made, "IFeature").Name = folder
            inside = folders_of(top_features(model)).get(folder, [])
            moved = [name for name in selected if name in inside]
            refused = [name for name in wanted if name not in inside]
    elif wanted:
        inside = list(existing.get(folder, []))
        for name in wanted:
            if name in inside:
                continue
            # Into the folder, else right after the folder's last feature: a
            # member's sketch moved in first, the member must follow it.
            tries = [(folder, _MOVE_TO_FOLDER)]
            if inside:
                tries.append((inside[-1], _MOVE_AFTER))
            for target, where in tries:
                adapter._attempt(
                    lambda n=name, t=target, w=where: model.Extension.ReorderFeature(
                        n, t, w
                    ),
                    default=False,
                )
                inside = folders_of(top_features(model)).get(folder, [])
                if name in inside:
                    break
        moved = [name for name in wanted if name in inside]
        refused = [name for name in wanted if name not in inside]
    return {"folder": folder, "moved": moved, "missing": missing, "refused": refused}


class SolidWorksFeatureTreeMixin:
    """Organize and audit the active part's feature tree."""

    @staticmethod
    def _adapter(obj: Any) -> Any:
        """Return the runtime adapter object for dynamic attribute access."""
        return cast(Any, obj)

    async def set_feature_folders(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Rename features and put them in folders.

        Args:
            params: ``rename`` (``{old: new}``) and ``folders`` (each
                ``{"name": ..., "features": [...]}``); renames go first, so
                folders may list the new names.

        Returns:
            AdapterResult[dict[str, Any]]: ``renamed``, ``unknown_names``,
            ``names_refused`` and ``folders`` (one result each).
        """
        adapter = self._adapter(self)
        if adapter.currentModel is None:
            return AdapterResult(
                status=AdapterResultStatus.ERROR, error="No active model"
            )

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            renamed: dict[str, str] = {}
            missing: list[str] = []
            refused: list[str] = []
            features = {str(f.Name): f for f in top_features(model)}
            for old, new in (params.get("rename") or {}).items():
                feature = features.get(old)
                if feature is None:
                    missing.append(old)
                    continue
                feature.Name = str(new)
                if str(feature.Name) == str(new):
                    renamed[old] = str(new)
                else:
                    refused.append(old)
            folders = [
                move_into_folder(
                    adapter, model, str(spec["name"]), list(spec.get("features") or [])
                )
                for spec in params.get("folders") or []
            ]
            return {
                "renamed": renamed,
                "unknown_names": missing,
                "names_refused": refused,
                "folders": folders,
            }

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("set_feature_folders", run),
        )

    async def check_feature_tree(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """What a specialist sees first in a part's tree, measured.

        Args:
            params: ``max_listed``, the most names listed per finding.

        Returns:
            AdapterResult[dict[str, Any]]: ``features`` (modelling features),
            ``by_type``, ``folders`` (name and count), ``outside_folders``,
            ``generic_names``, ``sketches`` (count per definition),
            ``not_fully_defined`` (sketch names), ``suppressed``, ``issues``
            and ``verdict``.
        """
        adapter = self._adapter(self)
        if adapter.currentModel is None:
            return AdapterResult(
                status=AdapterResultStatus.ERROR, error="No active model"
            )
        limit = int(params.get("max_listed") or 20)

        def run() -> dict[str, Any]:
            features = top_features(adapter.currentModel)
            folders = folders_of(features)
            in_folders = {name for names in folders.values() for name in names}
            modelling = _modelling(features)
            by_type: dict[str, int] = {}
            for feature in modelling:
                kind = str(feature.GetTypeName2())
                by_type[kind] = by_type.get(kind, 0) + 1
            names = [str(f.Name) for f in modelling]
            generic = [n for n in names if GENERIC_NAME.match(n)]
            outside = [
                n
                for f, n in zip(modelling, names, strict=True)
                if n not in in_folders
                and f.GetTypeName2() not in ("FtrFolder", *_ALWAYS_TOP)
                and not absorbed(f)
            ]
            states: dict[str, int] = {}
            loose: list[str] = []
            for feature in modelling:
                if feature.GetTypeName2() not in _SKETCH_TYPES:
                    continue
                sketch = _flagged(feature.GetSpecificFeature2(), "ISketch")
                state = DEFINITION.get(
                    int(
                        adapter._attempt(
                            lambda s=sketch: s.GetConstrainedStatus(), default=1
                        )
                    ),
                    "unknown",
                )
                states[state] = states.get(state, 0) + 1
                if state != "fully defined":
                    loose.append(str(feature.Name))
            suppressed = [
                str(f.Name)
                for f in modelling
                if adapter._attempt(lambda f=f: bool(f.IsSuppressed()), default=False)
            ]
            issues: list[str] = []
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
            plain = [n for n in outside if not n.endswith("___EndTag___")]
            if len(plain) > FOLDER_THRESHOLD:
                issues.append(
                    f"{len(plain)} features outside folders: group them "
                    "(skeleton, members, trims, plates, holes)"
                )
            if suppressed:
                issues.append(
                    f"{len(suppressed)} suppressed feature(s) left in the tree"
                )
            return {
                "features": len(modelling),
                "by_type": by_type,
                "folders": [
                    {"name": name, "features": len(inner)}
                    for name, inner in folders.items()
                ],
                "outside_folders": len(plain),
                "generic_names": generic[:limit],
                "generic_count": len(generic),
                "sketches": states,
                "not_fully_defined": loose[:limit],
                "suppressed": suppressed[:limit],
                "issues": issues,
                "verdict": "clean" if not issues else "needs work",
            }

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("check_feature_tree", run),
        )


#: Features that may stand outside any folder: the weldment's own feature.
_ALWAYS_TOP = frozenset({"WeldmentFeature"})
#: Children that leave a sketch at the top of the tree; any other child
#: (an extrusion, a cut) absorbs the sketch under itself.
_KEEPS_SKETCH_ON_TOP = frozenset(
    {
        "WeldMemberFeat",
        "RefPlane",
        "RefAxis",
        "RefPoint",
        "ProfileFeature",
        "3DProfileFeature",
    }
)


def absorbed(feature: Any) -> bool:
    """Whether a sketch sits under the feature that uses it (an extrusion's profile)."""
    if feature.GetTypeName2() not in _SKETCH_TYPES:
        return False
    children = as_list(feature.GetChildren())
    return any(
        str(_flagged(child, "IFeature").GetTypeName2()) not in _KEEPS_SKETCH_ON_TOP
        for child in children
    )


def _modelling(features: list[Any]) -> list[Any]:
    """The features a user made: no system folders, no origin, no main planes."""
    kept: list[Any] = []
    planes = 0
    for feature in features:
        kind = str(feature.GetTypeName2())
        if kind in SYSTEM_TYPES or str(feature.Name).endswith("___EndTag___"):
            continue
        if kind == "RefPlane" and planes < 3:
            planes += 1
            continue
        kept.append(feature)
    return kept
