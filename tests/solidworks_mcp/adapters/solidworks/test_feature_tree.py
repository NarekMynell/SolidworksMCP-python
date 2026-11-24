"""Tests for organizing and auditing the feature tree, against a fake tree."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
from solidworks_mcp.adapters.solidworks import feature_tree as tree
from solidworks_mcp.adapters.solidworks.feature_tree import SolidWorksFeatureTreeMixin


class _Feature:
    def __init__(
        self,
        name: str,
        kind: str,
        status: int = 3,
        children: tuple[Any, ...] = (),
        suppressed: bool = False,
    ) -> None:
        self.Name, self.kind = name, kind
        self.status, self.children, self.suppressed = status, children, suppressed
        self.next: Any = None
        self.model: Any = None
        self.inside: list[str] = []

    def GetTypeName2(self) -> str:
        return self.kind

    def GetNextFeature(self) -> Any:
        return self.next

    def GetChildren(self) -> tuple[Any, ...]:
        return self.children

    def IsSuppressed(self) -> bool:
        return self.suppressed

    def Select2(self, append: bool, _mark: int) -> bool:
        if not append:
            self.model.selected = []
        self.model.selected.append(self.Name)
        return True

    def GetSpecificFeature2(self) -> Any:
        if self.kind == "FtrFolder":
            return SimpleNamespace(
                GetFeatures=lambda: tuple(self.model.by_name(n) for n in self.inside)
            )
        return SimpleNamespace(GetConstrainedStatus=lambda: self.status)


class _Model:
    def __init__(self, features: list[_Feature], reorder: bool = True) -> None:
        self.features = features
        self.selected: list[str] = []
        self.reorder = reorder
        for f in features:
            f.model = self
        self._link()
        self.FeatureManager = SimpleNamespace(
            InsertFeatureTreeFolder2=self._folder,
        )
        self.Extension = SimpleNamespace(ReorderFeature=self._reorder)

    def _link(self) -> None:
        for a, b in zip(self.features, self.features[1:] + [None], strict=True):
            a.next = b

    def by_name(self, name: str) -> _Feature:
        return next(f for f in self.features if f.Name == name)

    def FeatureByName(self, name: str) -> Any:
        return next((f for f in self.features if f.Name == name), None)

    def FirstFeature(self) -> Any:
        return self.features[0] if self.features else None

    def ClearSelection2(self, _all: bool) -> None:
        self.selected = []

    def _folder(self, _kind: int) -> Any:
        if not self.selected:
            return None
        folder = _Feature("Folder1", "FtrFolder")
        folder.model = self
        folder.inside = list(self.selected)
        index = min(self.features.index(self.by_name(n)) for n in self.selected)
        self.features.insert(index, folder)
        self._link()
        return folder

    def _reorder(self, name: str, target: str, where: int) -> bool:
        if not self.reorder:
            return False
        folder = next(
            (
                f
                for f in self.features
                if f.kind == "FtrFolder" and (f.Name == target or target in f.inside)
            ),
            None,
        )
        if folder is None:
            return False
        folder.inside.append(name)
        return True


class _Adapter(SolidWorksFeatureTreeMixin):
    def __init__(self, model: Any) -> None:
        self.currentModel = model

    def _attempt(self, fn: Any, default: Any = None) -> Any:
        try:
            return fn()
        except Exception:
            return default

    def _handle_com_operation(self, _name: str, fn: Any) -> AdapterResult[Any]:
        try:
            return AdapterResult(status=AdapterResultStatus.SUCCESS, data=fn())
        except Exception as exc:
            return AdapterResult(status=AdapterResultStatus.ERROR, error=str(exc))


def _part() -> list[_Feature]:
    extrude = _Feature("BP-1 Base plate", "Extrusion")
    return [
        _Feature("Comments", "CommentsFolder"),
        _Feature("Solid Bodies", "SolidBodyFolder"),
        _Feature("Origin", "OriginProfileFeature"),
        _Feature("Front Plane", "RefPlane"),
        _Feature("Top Plane", "RefPlane"),
        _Feature("Right Plane", "RefPlane"),
        _Feature("Weldment", "WeldmentFeature"),
        _Feature(
            "Columns skeleton",
            "3DProfileFeature",
            children=(_Feature("Columns", "WeldMemberFeat"),),
        ),
        _Feature("Columns", "WeldMemberFeat"),
        _Feature("Sketch7", "ProfileFeature", status=2, children=(extrude,)),
        extrude,
        _Feature("Plane4", "RefPlane", suppressed=True),
    ]


def test_a_new_folder_takes_the_features() -> None:
    model = _Model(_part())
    adapter = _Adapter(model)
    placed = tree.move_into_folder(
        adapter, model, "Frame", ["Columns skeleton", "Columns", "Nope"]
    )
    assert placed == {
        "folder": "Frame",
        "moved": ["Columns skeleton", "Columns"],
        "missing": ["Nope"],
        "refused": [],
    }
    assert tree.folders_of(tree.top_features(model)) == {
        "Frame": ["Columns skeleton", "Columns"]
    }


def test_an_existing_folder_takes_more_and_reports_refusals() -> None:
    model = _Model(_part())
    adapter = _Adapter(model)
    tree.move_into_folder(adapter, model, "Frame", ["Columns skeleton"])
    placed = tree.move_into_folder(
        adapter, model, "Frame", ["Columns skeleton", "Columns"]
    )
    assert placed["moved"] == ["Columns skeleton", "Columns"]
    stuck = _Model(_part(), reorder=False)
    tree.move_into_folder(_Adapter(stuck), stuck, "Frame", ["Columns skeleton"])
    placed = tree.move_into_folder(_Adapter(stuck), stuck, "Frame", ["Columns"])
    assert placed["refused"] == ["Columns"]


def test_a_folder_solidworks_refuses_to_make() -> None:
    model = _Model(_part())
    model.FeatureManager.InsertFeatureTreeFolder2 = lambda _kind: None
    placed = tree.move_into_folder(_Adapter(model), model, "Frame", ["Columns"])
    assert placed["refused"] == ["Columns"]


def test_folders_skip_end_tags() -> None:
    model = _Model(_part())
    end = _Feature("Frame___EndTag___", "FtrFolder")
    model.features.append(end)
    end.model = model
    model._link()
    assert tree.folders_of(tree.top_features(model)) == {}


def test_absorbed_sketches_ride_inside_their_feature() -> None:
    features = _part()
    by_name = {f.Name: f for f in features}
    assert tree.absorbed(by_name["Sketch7"]) is True
    assert tree.absorbed(by_name["Columns skeleton"]) is False
    assert tree.absorbed(by_name["Columns"]) is False


@pytest.mark.asyncio
async def test_organize_renames_then_folds() -> None:
    model = _Model(_part())
    result = await _Adapter(model).set_feature_folders(
        {
            "rename": {"Plane4": "Splice plane", "Missing": "X"},
            "folders": [
                {"name": "Plates", "features": ["Splice plane", "BP-1 Base plate"]}
            ],
        }
    )
    assert result.status == AdapterResultStatus.SUCCESS
    data = result.data
    assert data["renamed"] == {"Plane4": "Splice plane"}
    assert data["unknown_names"] == ["Missing"]
    assert data["folders"][0]["moved"] == ["Splice plane", "BP-1 Base plate"]


@pytest.mark.asyncio
async def test_a_rename_solidworks_ignores_is_reported() -> None:
    class _Stubborn(_Feature):
        def __setattr__(self, key: str, value: Any) -> None:
            if key == "Name" and hasattr(self, "Name"):
                return
            super().__setattr__(key, value)

    features = _part()
    features[-1] = _Stubborn("Plane4", "RefPlane")
    model = _Model(features)
    result = await _Adapter(model).set_feature_folders(
        {"rename": {"Plane4": "Splice plane"}}
    )
    assert result.data["names_refused"] == ["Plane4"]


@pytest.mark.asyncio
async def test_audit_names_what_a_specialist_would_fix() -> None:
    model = _Model(_part())
    result = await _Adapter(model).check_feature_tree({"max_listed": 5})
    data = result.data
    assert data["features"] == 6
    assert data["generic_names"] == ["Sketch7", "Plane4"]
    assert data["not_fully_defined"] == ["Sketch7"]
    assert data["sketches"] == {"fully defined": 1, "under defined": 1}
    assert data["suppressed"] == ["Plane4"]
    assert data["outside_folders"] == 4
    assert data["verdict"] == "needs work"
    assert len(data["issues"]) == 3


@pytest.mark.asyncio
async def test_a_clean_tree_and_a_crowded_one() -> None:
    features = _part()[:9]
    model = _Model(features)
    tree.move_into_folder(
        _Adapter(model), model, "Frame", ["Columns skeleton", "Columns"]
    )
    clean = await _Adapter(model).check_feature_tree({})
    assert clean.data["verdict"] == "clean"
    assert clean.data["folders"] == [{"name": "Frame", "features": 2}]
    crowded = _part()[:6] + [
        _Feature(f"Brace {i}", "WeldMemberFeat")
        for i in range(tree.FOLDER_THRESHOLD + 1)
    ]
    result = await _Adapter(_Model(crowded)).check_feature_tree({})
    assert any("outside folders" in issue for issue in result.data["issues"])


@pytest.mark.asyncio
async def test_no_model_is_an_error() -> None:
    adapter = _Adapter(None)
    assert (await adapter.check_feature_tree({})).status == AdapterResultStatus.ERROR
    assert (await adapter.set_feature_folders({})).status == AdapterResultStatus.ERROR


def test_a_feature_refused_into_the_folder_goes_after_its_last_feature() -> None:
    model = _Model(_part())
    tree.move_into_folder(_Adapter(model), model, "Frame", ["Columns skeleton"])
    calls: list[int] = []
    original = model._reorder

    def into_folder_refused(name: str, target: str, where: int) -> bool:
        calls.append(where)
        return False if where == tree._MOVE_TO_FOLDER else original(name, target, where)

    model.Extension.ReorderFeature = into_folder_refused
    placed = tree.move_into_folder(_Adapter(model), model, "Frame", ["Columns"])
    assert calls == [tree._MOVE_TO_FOLDER, tree._MOVE_AFTER]
    assert placed["moved"] == ["Columns"]


def test_nothing_to_move_is_only_missing_names() -> None:
    model = _Model(_part())
    placed = tree.move_into_folder(_Adapter(model), model, "Frame", ["Ghost"])
    assert placed == {
        "folder": "Frame",
        "moved": [],
        "missing": ["Ghost"],
        "refused": [],
    }
