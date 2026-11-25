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


#: The ``IFeature`` methods these walks call; flagging only them is far
#: cheaper than flagging the whole interface on every feature of a big tree.
_FEATURE_MEMBERS = (
    "GetTypeName2",
    "GetNextFeature",
    "GetSpecificFeature2",
    "Select2",
    "GetChildren",
    "IsSuppressed",
)


def _feature(obj: Any) -> Any:
    """``obj`` with the ``IFeature`` methods used here flagged."""
    _sw_type_info.flag_members(obj, *_FEATURE_MEMBERS)
    return obj


def top_features(model: Any) -> list[Any]:
    """The model's top-level features in tree order.

    Args:
        model: The ``IModelDoc2``.

    Returns:
        list[Any]: The features, folders and the features in them alike.
    """
    found: list[Any] = []
    feature = model.FirstFeature()
    while feature is not None:
        feature = _feature(feature)
        found.append(feature)
        feature = feature.GetNextFeature()
    return found


def _contents(feature: Any) -> list[str]:
    """The names of the features in a folder feature."""
    folder = feature.GetSpecificFeature2()
    _sw_type_info.flag_members(folder, "GetFeatures")
    return [str(inner.Name) for inner in as_list(folder.GetFeatures())]


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
        if not name.endswith("___EndTag___"):
            folders[name] = _contents(feature)
    return folders


def _folder(model: Any, name: str) -> Any:
    """The folder feature of that name, or None."""
    feature = model.FeatureByName(name)
    if feature is None:
        return None
    feature = _feature(feature)
    return feature if feature.GetTypeName2() == "FtrFolder" else None


def move_into_folder(
    adapter: Any, model: Any, folder: str, names: list[str]
) -> dict[str, Any]:
    """Put features in a folder, making the folder when the tree has none of that name.

    Features are found by name and the folder's contents read back, so the
    cost follows the features moved, not the size of the tree.

    Args:
        adapter: The connected adapter.
        model: The part's ``IModelDoc2``.
        folder: The folder's name.
        names: Feature names, tree order.

    Returns:
        dict[str, Any]: ``folder``, ``moved`` (names now in it), ``missing``
        (no such feature) and ``refused`` (SolidWorks wouldn't move them).
    """
    found = {name: model.FeatureByName(name) for name in names}
    missing = [name for name, feature in found.items() if feature is None]
    wanted = [name for name in names if found[name] is not None]
    if not wanted:
        return {"folder": folder, "moved": [], "missing": missing, "refused": []}
    target = _folder(model, folder)
    note = None
    if target is not None and (blocking := _jumps_over(model, folder, wanted)):
        # Into the old folder would put them before a feature that changes
        # bodies (a pattern before the trim it copies came out untrimmed,
        # seen live): a new folder where they are keeps the order.
        note = f"kept after {blocking}, which changes the bodies they follow"
        folder, target = _free_folder_name(model, folder), None
    if target is None:
        model.ClearSelection2(True)
        for i, name in enumerate(wanted):
            _feature(found[name]).Select2(i > 0, 0)
        manager = model.FeatureManager
        _sw_type_info.flag_members(manager, "InsertFeatureTreeFolder2")
        made = adapter._attempt(
            lambda: manager.InsertFeatureTreeFolder2(_FOLDER_CONTAINING), default=None
        )
        model.ClearSelection2(True)
        if made is None:
            return {
                "folder": folder,
                "moved": [],
                "missing": missing,
                "refused": wanted,
            }
        made.Name = folder
        inside = _contents(_feature(made))
    else:
        inside = _contents(target)
        for name in wanted:
            if name in inside:
                continue
            # Into the folder, else right after its last feature (a member's
            # sketch moved in first: the member must follow it).
            landed = adapter._attempt(
                lambda n=name: bool(
                    model.Extension.ReorderFeature(n, folder, _MOVE_TO_FOLDER)
                ),
                default=False,
            )
            if not landed and inside:
                landed = adapter._attempt(
                    lambda n=name, last=inside[-1]: bool(
                        model.Extension.ReorderFeature(n, last, _MOVE_AFTER)
                    ),
                    default=False,
                )
            if landed:
                # Read back: a feature moved in lands at the folder's top, so
                # the last one inside is not always the one just moved (a
                # pattern placed after its own axis landed before its member).
                inside = _contents(_folder(model, folder))
        inside = _contents(_folder(model, folder))
    moved = [name for name in wanted if name in inside]
    refused = [name for name in wanted if name not in inside]
    placed: dict[str, Any] = {
        "folder": folder,
        "moved": moved,
        "missing": missing,
        "refused": refused,
    }
    if note:
        placed["note"] = note
    return placed


#: Feature types that change no body: moving one earlier changes no geometry.
_KEEPS_BODIES = SYSTEM_TYPES | {
    "RefPlane",
    "RefAxis",
    "RefPoint",
    "CoordSys",
    "ProfileFeature",
    "3DProfileFeature",
    "FtrFolder",
    "WeldmentFeature",
}


def _jumps_over(model: Any, folder: str, wanted: list[str]) -> str | None:
    """A feature that changes bodies between a folder's end and the features to move in.

    Moving a feature into a folder moves it up the tree, before everything
    after the folder; that is safe only past sketches, planes and axes.
    """
    order = top_features(model)
    names = [str(f.Name) for f in order]
    if folder not in names:
        return None
    start = names.index(folder)
    end = next(
        (i for i in range(start + 1, len(names)) if names[i].endswith("___EndTag___")),
        None,
    )
    moving = set(wanted)
    last = max((names.index(n) for n in wanted if n in names), default=-1)
    if end is None or last <= end:
        return None
    for i in range(end + 1, last):
        if names[i] not in moving and order[i].GetTypeName2() not in _KEEPS_BODIES:
            return names[i]
    return None


def last_folder_of(model: Any, names: list[str]) -> str | None:
    """The last folder in the tree holding any of the named features (or their bodies' features).

    A body name ``Joists IPE160[3]`` counts as its feature ``Joists IPE160``.
    """
    wanted = {n.rsplit("[", 1)[0] if n.endswith("]") else n for n in names}
    found = None
    for folder, contents in folders_of(top_features(model)).items():
        if wanted & set(contents):
            found = folder
    return found


def _free_folder_name(model: Any, wanted: str) -> str:
    """``wanted 2``, ``wanted 3``... the first no feature has."""
    count = 2
    while model.FeatureByName(f"{wanted} {count}") is not None:
        count += 1
    return f"{wanted} {count}"


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
            for old, new in (params.get("rename") or {}).items():
                feature = model.FeatureByName(old)
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
                sketch = feature.GetSpecificFeature2()
                _sw_type_info.flag_members(sketch, "GetConstrainedStatus")
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
        str(_feature(child).GetTypeName2()) not in _KEEPS_SKETCH_ON_TOP
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
