"""Tests for the weldments mixin, against a fake part that keeps state."""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
from solidworks_mcp.adapters.solidworks import io as io_module
from solidworks_mcp.adapters.solidworks import weldments
from solidworks_mcp.adapters.solidworks.weldments import (
    SolidWorksWeldmentsMixin,
    number,
)


def _values(array: Any) -> list[Any]:
    """The items of a COM array, whether a VARIANT or a plain tuple."""
    return list(getattr(array, "value", array))


class _Point:
    def __init__(self, xyz: tuple[float, float, float]) -> None:
        self.X, self.Y, self.Z = (v / 1000.0 for v in xyz)


class _Segment:
    def __init__(
        self, name: str, a: tuple, b: tuple, kind: int = 0, construction=False
    ) -> None:
        self.name, self.a, self.b, self.kind = name, a, b, kind
        self.ConstructionGeometry = construction

    def GetType(self) -> int:
        return self.kind

    def GetName(self) -> str:
        return self.name

    def GetStartPoint2(self) -> _Point:
        return _Point(self.a)

    def GetEndPoint2(self) -> _Point:
        return _Point(self.b)


class _Feature:
    def __init__(self, name: str, kind: str, specific: Any = None, subs=()) -> None:
        self.Name, self.kind, self.specific = name, kind, specific
        self.subs = list(subs)
        self.next: _Feature | None = None
        self.next_sub: _Feature | None = None
        self.CustomPropertyManager: Any = None

    def GetTypeName2(self) -> str:
        return self.kind

    def GetSpecificFeature2(self) -> Any:
        return self.specific

    def GetNextFeature(self) -> Any:
        return self.next

    def GetFirstSubFeature(self) -> Any:
        return self.subs[0] if self.subs else None

    def GetNextSubFeature(self) -> Any:
        return self.next_sub


class _Group:
    Segments: Any = None


class _Part:
    """A part with a sketch manager, a feature manager and a cut list."""

    def __init__(self) -> None:
        self.sketches: list[_Feature] = []
        self.lines: list[_Segment] | None = None
        self.weldment = False
        self.max_group = 99
        self.inserted: list[tuple[Any, ...]] = []
        self.bodies = 0
        self.first: _Feature | None = None
        manager = SimpleNamespace(ActiveSketch=None, AddToDB=False)
        manager.Insert3DSketch = self._toggle_3d
        manager.CreateLine = self._line
        self.SketchManager = manager
        self.FeatureManager = SimpleNamespace(
            InsertWeldmentFeature=self._weldment,
            CreateStructuralMemberGroup=_Group,
            InsertStructuralWeldment5=self._members,
        )
        self.units = 0
        self.Extension = SimpleNamespace(CreateMassProperty=self._mass)
        self.cleared = 0

    def ClearSelection2(self, _all: bool) -> None:
        self.cleared += 1

    def _toggle_3d(self, _rebuild: bool) -> None:
        if self.lines is None:
            self.lines = []
        else:
            sketch = SimpleNamespace(
                GetSketchSegments=lambda segs=tuple(self.lines): segs
            )
            self.sketches.append(
                _Feature(
                    f"3DSketch{len(self.sketches) + 1}", "3DProfileFeature", sketch
                )
            )
            self.lines = None

    def _line(self, *xyz: float) -> Any:
        if xyz[:3] == xyz[3:]:
            return None
        mm = [v * 1000.0 for v in xyz]
        segment = _Segment(
            f"Line{len(self.lines or []) + 1}", tuple(mm[:3]), tuple(mm[3:])
        )
        assert self.lines is not None
        self.lines.append(segment)
        return segment

    def FeatureByPositionReverse(self, _position: int) -> Any:
        return self.sketches[-1]

    def FeatureByName(self, name: str) -> Any:
        return next((f for f in self.sketches if f.Name == name), None)

    def IsWeldment(self) -> bool:
        return self.weldment

    def _weldment(self) -> None:
        self.weldment = True

    def _members(
        self, path: str, option: int, protrude: bool, groups: Any, size: str
    ) -> Any:
        found = _values(groups)
        sizes = [len(_values(g.Segments)) for g in found]
        self.inserted.append((path, size, sizes, found))
        if max(sizes) > self.max_group:
            return None
        self.bodies += sum(sizes)
        return _Feature(f"member({len(self.inserted)})", "WeldMemberFeat")

    def GetBodies2(self, _kind: int, _visible: bool) -> tuple:
        return tuple(range(self.bodies))

    def GraphicsRedraw2(self) -> None:
        pass

    def GetUserPreferenceIntegerValue(self, _setting: int) -> int:
        return self.units

    def FirstFeature(self) -> Any:
        return self.first

    def _mass(self) -> Any:
        mass = SimpleNamespace(Mass=0.0)

        def add(bodies: Any) -> bool:
            mass.Mass = 12.5 * len(_values(bodies))
            return True

        mass.AddBodies = add
        return mass


class _App:
    def __init__(self, root: str) -> None:
        self.root = root
        self.open: set[str] = set()
        self.closed: list[str] = []

    def GetUserPreferenceStringValue(self, _setting: int) -> str:
        return self.root

    def GetConfigurationNames(self, path: str) -> tuple[str, ...]:
        self.open.add(path)  # even if reading the sizes opened it, it's new
        return ("RHS60x60x4", "RHS100x100x5")

    def GetFirstDocument(self) -> Any:
        documents = [
            SimpleNamespace(
                GetPathName=lambda p=p: p, GetTitle=lambda p=p: Path(p).name
            )
            for p in ["C:/work/frame.SLDPRT", *sorted(self.open)]
        ]
        for document, after in zip(documents, [*documents[1:], None], strict=True):
            document.GetNext = lambda after=after: after
        return documents[0]

    def CloseDoc(self, title: str) -> None:
        self.closed.append(title)


class _Adapter(SolidWorksWeldmentsMixin):
    def __init__(self, root: str, doc_type: int = 1) -> None:
        self.currentModel = _Part()
        self.swApp = _App(root)
        self.doc_type = doc_type

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


@pytest.fixture
def root(tmp_path: Path) -> str:
    (tmp_path / "BSI").mkdir()
    (tmp_path / "BSI" / "shs.sldlfp").write_bytes(b"")
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weldments, "_doc_type", lambda a: a.doc_type)
    monkeypatch.setattr(io_module, "_dynamic", SimpleNamespace(Dispatch=lambda o: o))


_TRUSS = {
    "define": "none",
    "standard": "bsi",
    "profile_type": "SHS",
    "size": "rhs100x100x5",
    "segments": [
        [0, 0, 0, 2000, 0, 0],
        [2000, 0, 0, 2000, 1000, 0],
        [0, 0, 0, 0, 1000, 0],
    ],
}


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("56.31°", 56.31),
        ("1860,54", 1860.54),
        ("1,860.54", 1860.54),
        ("2", 2.0),
        ("", None),
    ],
)
def test_number_reads_resolved_properties(text: str, value: float | None) -> None:
    assert number(text) == value


@pytest.mark.asyncio
async def test_members_from_segments_make_a_weldment_and_close_the_profile(
    root: str,
) -> None:
    adapter = _Adapter(root)

    result = await adapter.create_structural_members(
        {**_TRUSS, "name": "Frame", "sketch_name": "Axes"}
    )

    assert result.is_success, result.error
    assert result.data["feature"] == "Frame"
    assert result.data["sketch"] == "Axes"
    assert (
        result.data["segments"],
        result.data["groups"],
        result.data["grouping"],
    ) == (3, 1, "auto")
    assert result.data["profile"]["size"] == "RHS100x100x5"
    assert result.data["bodies_after"] == 3
    part = adapter.currentModel
    assert part.weldment is True
    assert part.inserted[0][1] == "RHS100x100x5"
    assert adapter.swApp.closed == ["shs.sldlfp"]


@pytest.mark.asyncio
async def test_refused_chains_fall_back_to_one_group_per_segment(root: str) -> None:
    adapter = _Adapter(root)
    adapter.currentModel.max_group = 1

    result = await adapter.create_structural_members(_TRUSS)

    assert result.is_success, result.error
    assert (result.data["grouping"], result.data["groups"]) == ("each", 3)
    # the chain asked twice (a refusal is asked again once), then one group each
    assert [i[2] for i in adapter.currentModel.inserted] == [[3], [3], [1, 1, 1]]


@pytest.mark.asyncio
async def test_a_refusal_with_no_way_out_is_an_error(root: str) -> None:
    adapter = _Adapter(root)
    adapter.currentModel.max_group = 0

    result = await adapter.create_structural_members({**_TRUSS, "grouping": "each"})

    assert result.is_error
    assert "SolidWorks refused the structural member" in result.error


@pytest.mark.asyncio
async def test_members_on_an_existing_sketch_skip_construction_lines(root: str) -> None:
    adapter = _Adapter(root)
    segments = (
        _Segment("Line1", (0, 0, 0), (1000, 0, 0)),
        _Segment("Line2", (1000, 0, 0), (1000, 500, 0)),
        _Segment("Line3", (0, 0, 0), (0, 500, 0), construction=True),
        _Segment("Spline1", (0, 0, 0), (9, 9, 0), kind=3),
        _Segment("Arc1", (1000, 500, 0), (0, 500, 0), kind=1),
    )
    sketch = SimpleNamespace(GetSketchSegments=lambda: segments)
    adapter.currentModel.sketches.append(_Feature("Sketch1", "ProfileFeature", sketch))
    adapter.currentModel.weldment = True

    every = await adapter.create_structural_members(
        {
            "standard": "BSI",
            "profile_type": "shs",
            "size": "RHS60x60x4",
            "sketch": "Sketch1",
        }
    )
    some = await adapter.create_structural_members(
        {
            "standard": "BSI",
            "profile_type": "shs",
            "size": "RHS60x60x4",
            "sketch": "Sketch1",
            "segment_names": ["Line1"],
        }
    )
    missing = await adapter.create_structural_members(
        {
            "standard": "BSI",
            "profile_type": "shs",
            "size": "RHS60x60x4",
            "sketch": "Sketch1",
            "segment_names": ["Line9"],
        }
    )
    nowhere = await adapter.create_structural_members(
        {
            "standard": "BSI",
            "profile_type": "shs",
            "size": "RHS60x60x4",
            "sketch": "Sketch7",
        }
    )

    assert every.data["segments"] == 3 and every.data["groups"] == 1
    assert some.data["segments"] == 1
    assert "has no segment(s) Line9; it has Line1, Line2, Arc1" in missing.error
    assert "No sketch named 'Sketch7'" in nowhere.error


@pytest.mark.asyncio
async def test_a_sketch_with_nothing_to_follow_is_an_error(root: str) -> None:
    adapter = _Adapter(root)
    only_construction = (_Segment("Line1", (0, 0, 0), (1, 0, 0), construction=True),)
    sketch = SimpleNamespace(GetSketchSegments=lambda: only_construction)
    adapter.currentModel.sketches.append(_Feature("Sketch1", "ProfileFeature", sketch))

    result = await adapter.create_structural_members(
        {
            "standard": "BSI",
            "profile_type": "shs",
            "size": "RHS60x60x4",
            "sketch": "Sketch1",
        }
    )

    assert "has no lines or arcs" in result.error


@pytest.mark.asyncio
async def test_member_inputs_are_checked_before_anything_is_drawn(
    root: str, tmp_path: Path
) -> None:
    adapter = _Adapter(root)
    custom = tmp_path / "plate strip.sldlfp"
    custom.write_bytes(b"")

    bad_size = await adapter.create_structural_members({**_TRUSS, "size": "RHS999"})
    open_sketch = _Adapter(root)
    open_sketch.currentModel.SketchManager.ActiveSketch = object()
    busy = await open_sketch.create_structural_members(_TRUSS)
    same_point = await adapter.create_structural_members(
        {**_TRUSS, "segments": [[0, 0, 0, 0, 0, 0]]}
    )
    no_file = await adapter.create_structural_members(
        {
            "profile_path": str(tmp_path / "gone.sldlfp"),
            "size": "A",
            "segments": _TRUSS["segments"],
            "define": "none",
        }
    )
    wrong_config = await adapter.create_structural_members(
        {
            "profile_path": str(custom),
            "size": "B",
            "segments": _TRUSS["segments"],
            "define": "none",
        }
    )
    own = await adapter.create_structural_members(
        {
            "profile_path": str(custom),
            "size": "RHS60x60x4",
            "segments": _TRUSS["segments"],
            "define": "none",
        }
    )
    drawing = await _Adapter(root, doc_type=3).create_structural_members(_TRUSS)

    assert "No size 'RHS999'" in bad_size.error
    assert "close it with exit_sketch" in busy.error
    assert "refused the line" in same_point.error
    assert "Profile file not found" in no_file.error
    assert "has no size 'B'" in wrong_config.error
    assert own.data["profile"]["standard"] == "custom"
    assert "needs an open part" in drawing.error


def _cut_list_part(adapter: _Adapter, material: str = "Plain Carbon Steel") -> None:
    """Two cut-list items under Solid Bodies: 4 columns and 1 beam."""

    def manager(values: dict[str, str]) -> Any:
        def get6(
            name: str, _cached: bool, stored: Any, resolved: Any, *_rest: Any
        ) -> int:
            resolved.value = values.get(name, "")
            return 2

        return SimpleNamespace(Get6=get6)

    def folder(name: str, bodies: int, values: dict[str, str]) -> _Feature:
        feature = _Feature(
            name,
            "CutListFolder",
            SimpleNamespace(GetBodies=lambda: tuple(range(bodies))),
        )
        feature.CustomPropertyManager = manager({**values, "MATERIAL": material})
        return feature

    columns = folder(
        "HE400B<1>",
        4,
        {
            "Description": "HE400B",
            "QUANTITY": "4",
            "LENGTH": "12000",
            "ANGLE1": "0°",
            "ANGLE2": "0°",
            "TOTAL LENGTH": "48000",
        },
    )
    beam = folder(
        "IPE300<1>",
        1,
        {
            "Description": "IPE300",
            "QUANTITY": "1",
            "LENGTH": "9000",
            "ANGLE1": "45°",
            "ANGLE2": "45°",
            "TOTAL LENGTH": "9000",
        },
    )
    columns.next_sub = beam
    updated: list[bool] = []
    solid = _Feature(
        "Solid Bodies",
        "SolidBodyFolder",
        SimpleNamespace(UpdateCutList=lambda: updated.append(True)),
        [columns, beam],
    )
    comments = _Feature("Comments", "CommentsFolder")
    comments.next = solid
    adapter.currentModel.first = comments
    adapter.currentModel.updated = updated  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_the_cut_list_reads_items_lengths_and_masses(
    root: str, tmp_path: Path
) -> None:
    adapter = _Adapter(root)
    _cut_list_part(adapter)
    out = tmp_path / "out" / "cut.csv"

    result = await adapter.get_cut_list(export_path=str(out))

    assert result.is_success, result.error
    columns, beam = result.data["items"]
    assert columns == {
        "item": 1,
        "name": "HE400B<1>",
        "description": "HE400B",
        "quantity": 4,
        "length_mm": 12000.0,
        "angle1_deg": 0.0,
        "angle2_deg": 0.0,
        "total_length_mm": 48000.0,
        "profile_total_length_mm": 48000.0,
        "material": "Plain Carbon Steel",
        "mass_each_kg": 12.5,
        "mass_total_kg": 50.0,
    }
    assert beam["angle1_deg"] == 45.0
    assert (
        result.data["pieces"],
        result.data["total_length_mm"],
        result.data["total_mass_kg"],
    ) == (5, 57000.0, 62.5)
    assert "warning" not in result.data
    assert adapter.currentModel.updated == [True]  # type: ignore[attr-defined]
    with open(out, encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["name"] for r in rows] == ["HE400B<1>", "IPE300<1>"]


@pytest.mark.asyncio
async def test_the_cut_list_in_inches_is_given_in_mm_and_warns_without_material(
    root: str,
) -> None:
    adapter = _Adapter(root)
    _cut_list_part(adapter, material="Material <not specified>")
    adapter.currentModel.units = 3

    result = await adapter.get_cut_list(update=False)

    assert result.data["items"][1]["length_mm"] == pytest.approx(9000 * 25.4)
    assert "default density" in result.data["warning"]
    assert adapter.currentModel.updated == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_no_cut_list_no_csv_and_no_part_are_errors(root: str) -> None:
    adapter = _Adapter(root)
    adapter.currentModel.first = _Feature("Comments", "CommentsFolder")

    empty = await adapter.get_cut_list()
    bad_path = await adapter.get_cut_list(export_path="cut.xlsx")
    assembly = await _Adapter(root, doc_type=2).get_cut_list()
    nothing = _Adapter(root)
    nothing.currentModel = None
    unopened = await nothing.get_cut_list()

    assert "not a weldment" in empty.error
    assert "must end in .csv" in bad_path.error
    assert "needs an open part" in assembly.error
    assert unopened.error == "No active model"


@pytest.mark.asyncio
async def test_listing_the_library(root: str, tmp_path: Path) -> None:
    adapter = _Adapter(root)

    standards = await adapter.list_weldment_profiles()
    types = await adapter.list_weldment_profiles("bsi")
    sizes = await adapter.list_weldment_profiles("BSI", "shs")
    found = await adapter.list_weldment_profiles(query="100x100", limit=1)
    all_found = await adapter.list_weldment_profiles(query="RHS", limit=1)
    nowhere = await _Adapter(str(tmp_path / "missing")).list_weldment_profiles()

    assert standards.data["standards"] == [{"standard": "BSI", "types": ["shs"]}]
    assert types.data["types"] == [
        {"type": "shs", "sizes": ["RHS60x60x4", "RHS100x100x5"]}
    ]
    assert sizes.data["sizes"] == ["RHS60x60x4", "RHS100x100x5"]
    assert [m["size"] for m in found.data["matches"]] == ["RHS100x100x5"]
    assert found.data["truncated"] is False and all_found.data["truncated"] is True
    assert "No weldment profile folder" in nowhere.error
    # The library is read once and kept while the folder setting holds.
    assert adapter._profile_library() is adapter._profile_library()


@pytest.mark.asyncio
async def test_a_profile_the_user_had_open_stays_open(root: str) -> None:
    adapter = _Adapter(root)
    adapter.swApp.open.add(str(Path(root) / "BSI" / "shs.sldlfp"))

    result = await adapter.create_structural_members(_TRUSS)

    assert result.is_success
    assert adapter.swApp.closed == []


# ---- Trim/Extend ---------------------------------------------------------------


class _Body:
    """A solid body: a name, a centre of mass (m) and a volume (m3)."""

    def __init__(self, name: str, centre: tuple, volume: float) -> None:
        self.Name, self.centre, self.volume = name, centre, volume

    def GetMassProperties(self, _density: float) -> tuple:
        return (*self.centre, self.volume, 1.0, self.volume)


class _TrimPart:
    """A weldment of named members whose Trim/Extend renames what it cuts.

    ``cuts`` says what the trim does to each body it is given: a volume
    change (m3) and a shift of its centre (m); a body left out is untouched
    and keeps its name, as SolidWorks does.
    """

    def __init__(self, members: dict[str, int], cuts: dict[str, tuple] | None = None):
        self.bodies: list[_Body] = []
        self.features: list[_Feature] = []
        for index, (member, count) in enumerate(members.items()):
            owned = []
            for n in range(1, count + 1):
                name = f"{member}[{n}]" if count > 1 else member
                body = _Body(name, (float(index), float(n), 0.0), 1e-3)
                self.bodies.append(body)
                owned.append(body)
            feature = _Feature(member, "WeldMemberFeat")
            feature.GetFaces = lambda owned=owned: tuple(
                SimpleNamespace(GetBody=lambda b=b: b) for b in owned
            )
            self.features.append(feature)
        sketch = _Feature("3DSketch1", "3DProfileFeature")
        for feature, after in zip(
            [sketch, *self.features], [*self.features, None], strict=True
        ):
            feature.next = after
        self.first = sketch
        self.cuts = cuts if cuts is not None else {}
        self.calls: list[tuple] = []
        self.refuse = False
        self.FeatureManager = SimpleNamespace(InsertWeldmentTrimFeature2=self._trim)

    def GetBodies2(self, _kind: int, _visible: bool) -> tuple:
        return tuple(self.bodies)

    def FeatureByName(self, name: str) -> Any:
        return next((f for f in self.features if f.Name == name), None)

    def FirstFeature(self) -> Any:
        return self.first

    def _trim(self, corner, options, gap, members, boundary) -> Any:
        names = [b.Name for b in _values(members)]
        self.calls.append(
            (corner, options, gap, names, [b.Name for b in _values(boundary)])
        )
        if self.refuse:
            return None
        renamed = 0
        for body in _values(members):
            change, shift = self.cuts.get(body.Name, (-1e-5, 0.01))
            if (change, shift) == (0.0, 0.0):
                continue
            renamed += 1
            body.Name = f"Trim/Extend1[{renamed}]"
            body.volume += change
            body.centre = (body.centre[0] + shift, *body.centre[1:])
        return _Feature("Trim/Extend1", "WeldCornerFeat")


def _trimming(part: _TrimPart) -> _Adapter:
    adapter = _Adapter("")
    adapter.currentModel = part  # type: ignore[assignment]
    return adapter


@pytest.mark.asyncio
async def test_joists_are_coped_to_the_beams_and_renamed() -> None:
    part = _TrimPart({"Beams": 2, "Joists": 3})
    adapter = _trimming(part)

    result = await adapter.create_trim_extend(
        {
            "members": ["Joists"],
            "boundary": ["Beams"],
            "cut": "coped",
            "weld_gap_mm": 5,
            "name": "Joists to beams",
        }
    )

    assert result.is_success, result.error
    (call,) = part.calls
    assert call == (
        4,  # swEndConditionTrim
        1 | 4 | 8,  # allow extension, coped cut, weld gap
        0.005,
        ["Joists[1]", "Joists[2]", "Joists[3]"],
        ["Beams[1]", "Beams[2]"],
    )
    data = result.data
    assert data["feature"] == "Joists to beams"
    assert [(m["name"], m["now"]) for m in data["members"]] == [
        ("Joists[1]", "Trim/Extend1[1]"),
        ("Joists[2]", "Trim/Extend1[2]"),
        ("Joists[3]", "Trim/Extend1[3]"),
    ]
    assert {m["volume_change_mm3"] for m in data["members"]} == {-10000.0}
    assert data["unchanged"] == [] and "warning" not in data
    assert data["boundary"] == ["Beams[1]", "Beams[2]"]
    assert data["bodies_after"] == 5


@pytest.mark.asyncio
async def test_a_mitre_keeps_the_volume_but_moves_the_centre() -> None:
    """A mitred end gains outside what it loses inside: still a change."""
    part = _TrimPart({"Rail A": 1, "Rail B": 1}, {"Rail A": (0.0, 0.015)})
    adapter = _trimming(part)

    result = await adapter.create_trim_extend(
        {"members": ["Rail A"], "boundary": ["Rail B"], "corner_type": "miter"}
    )

    assert part.calls[0][:3] == (1, 1 | 2, 0.0)
    (member,) = result.data["members"]
    assert (member["volume_change_mm3"], member["changed"]) == (0.0, True)
    assert result.data["unchanged"] == []


@pytest.mark.asyncio
async def test_a_member_the_boundary_does_not_reach_is_named() -> None:
    part = _TrimPart({"Beams": 1, "Joists": 2}, {"Joists[2]": (0.0, 0.0)})
    adapter = _trimming(part)

    result = await adapter.create_trim_extend(
        {
            "members": ["Joists[1]", "Joists[2]"],
            "boundary": ["Beams"],
            "allow_extension": False,
        }
    )

    assert part.calls[0][1] == 0  # simple cut, no extension, no gap
    assert result.data["unchanged"] == ["Joists[2]"]
    assert result.data["members"][1]["now"] == "Joists[2]"
    assert "1 member(s) didn't change: Joists[2]" in result.data["warning"]


@pytest.mark.asyncio
async def test_unreadable_masses_are_reported_as_unknown() -> None:
    part = _TrimPart({"Beams": 1, "Joists": 1})
    for body in part.bodies:
        body.GetMassProperties = lambda _d: None  # type: ignore[method-assign]
    adapter = _trimming(part)

    result = await adapter.create_trim_extend(
        {"members": ["Joists"], "boundary": ["Beams"]}
    )

    (member,) = result.data["members"]
    assert (member["now"], member["volume_change_mm3"], member["changed"]) == (
        None,
        None,
        None,
    )
    assert result.data["unchanged"] == []


@pytest.mark.parametrize(
    ("params", "message"),
    [
        (
            {"members": ["Purlins"], "boundary": ["Beams"]},
            "No structural member or body named 'Purlins'; the part has the members "
            "Beams, Joists; its bodies are Beams[1], Beams[2], Joists[1], Joists[2]",
        ),
        (
            {"members": ["Joists"], "boundary": ["Joists[2]"]},
            "Joists[2] can't be both trimmed and the boundary",
        ),
        (
            {"members": ["Joists"], "boundary": ["Beams[1]"], "corner_type": "butt1"},
            "A butt1 corner joins two members: give one member body and one "
            "boundary body (got 2 and 1)",
        ),
    ],
)
@pytest.mark.asyncio
async def test_trim_refusals(params: dict[str, Any], message: str) -> None:
    part = _TrimPart({"Beams": 2, "Joists": 2})

    result = await _trimming(part).create_trim_extend(params)

    assert result.error == message
    assert part.calls == []


@pytest.mark.asyncio
async def test_a_trim_solidworks_refuses_and_a_big_frame_and_no_part() -> None:
    refused = _TrimPart({"Beams": 1, "Joists": 1})
    refused.refuse = True
    big = _TrimPart({"Purlins": 30})
    drawing = _Adapter("", doc_type=3)

    no = await _trimming(refused).create_trim_extend(
        {"members": ["Joists"], "boundary": ["Beams"]}
    )
    unknown = await _trimming(big).create_trim_extend(
        {"members": ["Rafters"], "boundary": ["Purlins"]}
    )
    wrong = await drawing.create_trim_extend(
        {"members": ["Joists"], "boundary": ["Beams"]}
    )

    assert "SolidWorks refused the Trim/Extend" in (no.error or "")
    assert unknown.error == (
        "No structural member or body named 'Rafters'; the part has the members "
        "Purlins and 30 bodies, named '<member>[n]'"
    )
    assert "create_trim_extend needs an open part" in (wrong.error or "")
