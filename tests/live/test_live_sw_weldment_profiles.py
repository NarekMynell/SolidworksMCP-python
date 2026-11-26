"""Weldment profiles built from a materials catalog, checked on real SolidWorks.

``data/weldment_profiles/opus_profiles.json`` holds 11 real catalog profiles:
a rectangular, a round and an oval tube, an angle, a channel, an I-beam, a
tee, a flat, and a round, a square and a hex bar, each with the area its
catalog gives.

1. ``build_weldment_profiles`` builds all 11 in one call into a temporary
   library root and writes the results file beside the spec. Each file is
   opened and read: a library feature with the sketch under it, the
   outline's lines and arcs, a sketch point at every pierce point, the
   spec's properties, one configuration. Each becomes a structural member
   along a 1000 mm line (``create_structural_members`` with
   ``profile_path``): its volume over 1000 equals the catalog's area within
   0.2 %, and its cut-list item carries the profile's properties. The
   library reader lists the standard ``Opus`` with every type and size.
2. A second call skips what exists, ``overwrite`` builds it again, a
   profile SolidWorks refuses fails alone, and no document is left open, an
   unsaved one included; the settings Fully Define Sketch switches off are
   as they were. The default root is the first Weldment Profiles folder.
3. The catalog again with ``define: none``: the time of a profile without
   Fully Define Sketch, and the definition it reports.
4. ``InsertStructuralWeldment5`` itself refuses a profile of one
   configuration under that configuration's name, ``Default`` or another
   (as a template in another language gives), and takes it with none; the
   tool puts the renamed one in as a member by its file name.
5. A member of the RHS pierced at the sketch point on its box's corner
   (``IStructuralMemberFeatureData.LocateProfilePoint``) moves 30 and 40 mm
   across its line, the corner along it.

Each test writes its numbers to its section of
``docs/proofs/weldment-profiles.json`` before it asserts them.
Collected only with ``SOLIDWORKS_MCP_RUN_REAL_INTEGRATION=1`` on Windows.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.live.evidence import evidence

pytestmark = pytest.mark.asyncio

SPEC = Path(__file__).parent / "data" / "weldment_profiles" / "opus_profiles.json"
RECEIPT = Path(__file__).parents[2] / "docs" / "proofs" / "weldment-profiles.json"
#: The body's volume per mm and the catalog's area may differ by this share.
TOLERANCE = 0.002
MEMBER_MM = 1000.0
#: A sketch point this close to a pierce point is at it, mm.
AT_POINT_MM = 1e-3


async def _tools(adapter: Any) -> dict[str, Any]:
    """Every tool, registered against ``adapter``: name -> function."""
    from fastmcp import FastMCP

    from solidworks_mcp.tools import register_tools

    mcp = FastMCP("weldment-profiles-live")
    await register_tools(mcp, adapter, {})
    return {t.name: t.fn for t in await mcp.list_tools()}


def _ok(reply: dict[str, Any]) -> dict[str, Any]:
    assert reply["status"] == "success", reply
    return reply


def _spec_copy(tmp_path: Path, profiles: list[dict[str, Any]] | None = None) -> Path:
    """The fixture (or ``profiles``) as a spec in ``tmp_path``, so its results land there."""
    spec = tmp_path / "opus_profiles.json"
    if profiles is None:
        shutil.copyfile(SPEC, spec)
    else:
        spec.write_text(json.dumps({"profiles": profiles}), encoding="utf-8")
    return spec


def _catalog() -> list[dict[str, Any]]:
    return list(json.loads(SPEC.read_text(encoding="utf-8"))["profiles"])


def _record(adapter: Any, section: str, passed: bool, data: dict[str, Any]) -> None:
    """Write one test's numbers into its section of the receipt."""
    from solidworks_mcp.adapters.solidworks.com_utils import (
        DEFAULT_COM_SETTLE_SECONDS,
        DEFAULT_DOC_SETTLE_SECONDS,
        settle_seconds,
    )

    try:
        old = json.loads(RECEIPT.read_text(encoding="utf-8"))
        tests = old.get("tests") if isinstance(old.get("tests"), dict) else {}
    except (OSError, ValueError, AttributeError):
        tests = {}
    tests[section] = {
        "ran": datetime.now(UTC).isoformat(timespec="seconds"),
        "solidworks": str(adapter.swApp.RevisionNumber()),
        # The pauses after each SolidWorks call and each new document: every
        # profile's seconds include two document pauses.
        "pauses_s": {
            "after_call": settle_seconds(
                "SOLIDWORKS_MCP_COM_SETTLE_SECONDS", DEFAULT_COM_SETTLE_SECONDS
            ),
            "after_document": settle_seconds(
                "SOLIDWORKS_MCP_DOC_SETTLE_SECONDS", DEFAULT_DOC_SETTLE_SECONDS
            ),
        },
        "passed": passed,
        **data,
    }
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(
        json.dumps(
            {"check": "tests/live/test_live_sw_weldment_profiles.py", "tests": tests},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _feature(raw: Any) -> Any:
    from solidworks_mcp.adapters import sw_type_info

    return sw_type_info.flagged(raw, "IFeature") if raw is not None else None


def _read_profile(adapter: Any, file: str) -> dict[str, Any]:
    """What a built profile file holds, read from it in SolidWorks (read-only)."""
    from solidworks_mcp.adapters import sw_type_info
    from solidworks_mcp.adapters.solidworks.com_utils import as_list
    from solidworks_mcp.adapters.solidworks.io import _byref_int
    from solidworks_mcp.adapters.solidworks.weldments import _property

    app = adapter.swApp
    errors, warnings = _byref_int(), _byref_int()
    model = app.OpenDoc6(file, 1, 1 | 2, "", errors, warnings)  # silent, read-only
    assert model is not None, f"SolidWorks didn't open {file}: {errors.value}"
    sw_type_info.flag_doc(model, 1)
    try:
        library, sketch = [], None
        feature = _feature(model.FirstFeature())
        while feature is not None:
            if feature.GetTypeName2() == "LibraryFeature":
                sub = _feature(feature.GetFirstSubFeature())
                while sub is not None:
                    library.append(str(sub.GetTypeName2()))
                    if sub.GetTypeName2() == "ProfileFeature":
                        sketch = sw_type_info.flagged(
                            sub.GetSpecificFeature2(), "ISketch"
                        )
                    sub = _feature(sub.GetNextSubFeature())
            feature = _feature(feature.GetNextFeature())
        kinds = [
            int(sw_type_info.flagged(s, "ISketchSegment").GetType())
            for s in as_list(sketch.GetSketchSegments() if sketch else None)
        ]
        points = [
            (float(p.X) * 1000.0, float(p.Y) * 1000.0)
            for p in (
                sw_type_info.flagged(raw, "ISketchPoint")
                for raw in as_list(sketch.GetSketchPoints2() if sketch else None)
            )
        ]
        manager = sw_type_info.flagged(
            model.Extension.CustomPropertyManager(""), "ICustomPropertyManager"
        )
        names = [str(n) for n in as_list(manager.GetNames())]
        return {
            "under_library_feature": library,
            "lines": kinds.count(0),
            "arcs": kinds.count(1),
            "points": points,
            "properties": {n: _property(adapter, manager, n) for n in names},
            "configurations": [str(c) for c in as_list(model.GetConfigurationNames())],
        }
    finally:
        app.CloseDoc(str(model.GetPathName()))


def _file_checks(found: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """The built file against its spec entry."""
    from solidworks_mcp.adapters.solidworks.weldment_profiles import (
        check_outline,
        pierce_points,
    )

    outline = check_outline(entry["outline"])
    wanted = pierce_points(outline)
    missing = [
        name
        for name, (x, y) in wanted
        if not any(
            abs(x - px) <= AT_POINT_MM and abs(y - py) <= AT_POINT_MM
            for px, py in found["points"]
        )
    ]
    counts = outline.counts()
    return {
        "library_feature_holds_the_sketch": found["under_library_feature"]
        == ["ProfileFeature"],
        "lines_and_arcs": [found["lines"], found["arcs"]],
        "outline_lines_and_arcs": [counts["lines"], counts["arcs"]],
        "pierce_points": len(wanted),
        "pierce_points_missing": missing,
        "properties_as_given": found["properties"] == entry["properties"],
        "configurations": found["configurations"],
    }


def _file_ok(checks: dict[str, Any]) -> bool:
    return bool(
        checks["library_feature_holds_the_sketch"]
        and checks["lines_and_arcs"] == checks["outline_lines_and_arcs"]
        and not checks["pierce_points_missing"]
        and checks["properties_as_given"]
        and len(checks["configurations"]) == 1
    )


def _cut_list_properties(adapter: Any) -> list[dict[str, str]]:
    """Every cut-list item's custom properties in the active part."""
    from solidworks_mcp.adapters import sw_type_info
    from solidworks_mcp.adapters.solidworks.com_utils import as_list
    from solidworks_mcp.adapters.solidworks.weldments import (
        _cut_list_folders,
        _property,
    )

    items = []
    for folder in _cut_list_folders(adapter, adapter.currentModel, update=True):
        manager = sw_type_info.flagged(
            folder.CustomPropertyManager, "ICustomPropertyManager"
        )
        names = [str(n) for n in as_list(manager.GetNames())]
        items.append({n: _property(adapter, manager, n) for n in names})
    return items


async def _member(
    tools: dict[str, Any], adapter: Any, file: str, size: str, expected: float
) -> tuple[float, list[dict[str, str]]]:
    """A member of the profile along a 1000 mm line: volume over 1000 (mm²), cut list."""
    _ok(await tools["create_part"]({"name": "profile probe"}))
    try:
        _ok(
            await tools["create_structural_members"](
                {
                    "size": size,
                    "profile_path": file,
                    "segments": [[0, 0, 0, 0, MEMBER_MM, 0]],
                    "name": "Probe",
                    "define": "none",
                }
            )
        )
        report = _ok(
            await tools["verify_model"](
                {
                    "volume_mm3": expected * MEMBER_MM,
                    "relative_tolerance": TOLERANCE,
                }
            )
        )
        area = float(report["measured"]["volume_mm3"]) / MEMBER_MM
        return area, _cut_list_properties(adapter)
    finally:
        _ok(await tools["close_model"]({"save": False}))


def _deviation(area: float, expected: float) -> float:
    return round((area - expected) / expected * 100, 5)


async def test_a_catalog_becomes_profiles_whose_members_have_its_areas(
    scratch: Any, tmp_path: Path
) -> None:
    from solidworks_mcp.adapters.solidworks.com_utils import as_list
    from solidworks_mcp.adapters.solidworks.weldment_library import ProfileLibrary

    adapter = scratch.adapter
    tools = await _tools(adapter)
    catalog = _catalog()
    spec = _spec_copy(tmp_path)
    root = tmp_path / "Weldment Profiles"

    built = _ok(
        await tools["build_weldment_profiles"](
            {"spec_file": str(spec), "root": str(root)}
        )
    )
    profiles = built["profiles"]
    evidence(
        "11 catalog profiles built in one call",
        {k: profiles[k] for k in ("root", "built", "skipped", "failed", "seconds")},
        call="build_weldment_profiles({spec_file, root})",
    )
    results = json.loads(Path(profiles["results_file"]).read_text(encoding="utf-8"))

    rows = []
    for entry, item in zip(catalog, results["profiles"], strict=True):
        file = str(root.joinpath(*entry["path"].split("/")))
        size = os.path.splitext(os.path.basename(file))[0]
        exists = os.path.basename(file) in os.listdir(os.path.dirname(file))
        checks = _file_checks(_read_profile(adapter, file), entry)
        area, cut_list = await _member(tools, adapter, file, size, entry["area_mm2"])
        rows.append(
            {
                "path": entry["path"],
                "status": item["status"],
                "seconds": item["seconds"],
                "definition": item["definition"],
                "sha256": item["sha256"],
                "spelling_as_asked": exists,
                "file": checks,
                "file_ok": _file_ok(checks),
                "area_mm2": entry["area_mm2"],
                "member_volume_per_mm": round(area, 4),
                "deviation_percent": _deviation(area, entry["area_mm2"]),
                "cut_list_has_the_properties": len(cut_list) == 1
                and all(
                    cut_list[0].get(k) == v for k, v in entry["properties"].items()
                ),
            }
        )

    def sizes_of(path: str) -> list[str]:
        return [str(n) for n in as_list(adapter.swApp.GetConfigurationNames(path))]

    library = ProfileLibrary([str(root)], sizes_of)
    types = {t: library.sizes("Opus", t) for t in library.types("Opus")}
    expected_types: dict[str, list[str]] = {}
    for entry in catalog:
        _, kind, name = entry["path"].split("/")
        expected_types.setdefault(kind, []).append(os.path.splitext(name)[0])
    evidence("each profile: its file, its member and its cut list", rows)
    evidence("the library reader on the new root", types)

    checks = {
        "11 built": (profiles["built"], profiles["skipped"], profiles["failed"])
        == (11, 0, 0),
        "results in spec order": [r["path"] for r in results["profiles"]]
        == [p["path"] for p in catalog],
        "files as asked": all(r["spelling_as_asked"] and r["file_ok"] for r in rows),
        "areas within 0.2 %": all(
            abs(r["deviation_percent"]) <= TOLERANCE * 100 for r in rows
        ),
        "cut lists carry the properties": all(
            r["cut_list_has_the_properties"] for r in rows
        ),
        "library reader": library.standards() == ["Opus"]
        and types == {k: sorted(v) for k, v in expected_types.items()},
    }
    passed = all(checks.values())
    _record(
        adapter,
        "catalog",
        passed,
        {
            "define": "dimensions",
            "tolerance_percent": TOLERANCE * 100,
            "checks": checks,
            "seconds_total": profiles["seconds"],
            "library": {"standards": library.standards(), "types": types},
            "profiles": rows,
        },
    )
    assert passed, {k: v for k, v in checks.items() if not v}


async def test_skip_overwrite_and_a_profile_solidworks_refuses(
    scratch: Any, tmp_path: Path
) -> None:
    from solidworks_mcp.adapters.solidworks.weldment_profiles import (
        _open_documents,
        _read_toggles,
        _restore_toggles,
        profiles_root,
    )

    adapter = scratch.adapter
    tools = await _tools(adapter)
    open_before = sorted(_open_documents(adapter))
    # Fully Define Sketch switches these settings off: on first, so putting
    # them back shows; the user's own values come back at the end.
    users = _read_toggles(adapter)
    for toggle in users:
        adapter.swApp.SetUserPreferenceToggle(toggle, True)
    toggles_before = _read_toggles(adapter)
    catalog = {p["shape"]: p for p in _catalog()}
    root = tmp_path / "Weldment Profiles"
    angle = catalog["angle"]
    # An outline that crosses itself at (10, 0): closed and counter-clockwise
    # overall, so only SolidWorks' own check of the sketch can tell.
    crossed = copy.deepcopy(angle)
    crossed["path"] = "Opus/angles/crossed.sldlfp"
    corners = [[0, 0], [30, 0], [30, 30], [10, 30], [10, -10], [0, -10]]
    crossed["outline"] = {
        "outer": [
            {"k": "line", "a": corners[i], "b": corners[(i + 1) % len(corners)]}
            for i in range(len(corners))
        ],
        "holes": [],
    }
    crossed.pop("area_mm2")
    crossed.pop("mass_per_m")
    spec = _spec_copy(tmp_path, [angle, catalog["flat_bar"], crossed])
    paths = [angle["path"], catalog["flat_bar"]["path"]]

    async def build(**extra: Any) -> dict[str, Any]:
        return _ok(
            await tools["build_weldment_profiles"](
                {"spec_file": str(spec), "root": str(root), **extra}
            )
        )

    try:
        first = await build(only=paths)
        again = await build(only=paths)
        redone = await build(only=[paths[0]], overwrite=True, define="none")
        after_redo = json.loads(
            Path(redone["profiles"]["results_file"]).read_text(encoding="utf-8")
        )
        mixed = await build(overwrite=True)
        open_after = sorted(_open_documents(adapter))
        toggles_after = _read_toggles(adapter)
    finally:
        _restore_toggles(adapter, users)
    setting = str(adapter.swApp.GetUserPreferenceStringValue(29))
    first_folder = [p.strip() for p in setting.split(";") if p.strip()][0]

    def run(reply: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {k: i[k] for k in ("path", "status", "seconds", "sha256", "error")}
            for i in reply["profiles"]["items"]
        ]

    redone_row = next(p for p in after_redo["profiles"] if p["path"] == paths[0])
    refused = mixed["profiles"]["items"][2]
    checks = {
        "first builds both": [i["status"] for i in first["profiles"]["items"]]
        == ["built", "built"],
        "again skips both, files unchanged": [
            (i["status"], i["sha256"]) for i in again["profiles"]["items"]
        ]
        == [("skipped", i["sha256"]) for i in first["profiles"]["items"]],
        "overwrite with define none builds it under defined": (
            redone["profiles"]["items"][0]["status"],
            redone_row["definition"],
            redone_row["define"],
        )
        == ("built", "under defined", "none"),
        "the crossed outline fails alone": [
            i["status"] for i in mixed["profiles"]["items"]
        ]
        == ["built", "built", "error"]
        and "check of the sketch as a profile" in str(refused["error"]),
        "no file for the failed profile": not (
            root / "Opus" / "angles" / "crossed.sldlfp"
        ).exists(),
        "the results file lists all three": [
            p["path"]
            for p in json.loads(
                Path(mixed["profiles"]["results_file"]).read_text(encoding="utf-8")
            )["profiles"]
        ]
        == [*paths, crossed["path"]],
        "no document left open, unsaved ones included": open_after == open_before,
        "settings put back": toggles_after == toggles_before
        and all(toggles_before.values()),
        "default root is the first Weldment Profiles folder": profiles_root(
            adapter, None
        )
        == os.path.abspath(first_folder),
    }
    evidence("skip, overwrite and a refused profile", checks)
    passed = all(checks.values())
    _record(
        adapter,
        "skip_overwrite_refused",
        passed,
        {
            "checks": checks,
            "first": run(first),
            "again": run(again),
            "overwrite_define_none": run(redone),
            "overwrite_all": run(mixed),
            "warning": mixed.get("warning"),
            "open_documents_before": open_before,
            "open_documents_after": open_after,
            "settings_before": {str(k): v for k, v in toggles_before.items()},
            "settings_after": {str(k): v for k, v in toggles_after.items()},
            "users_settings_restored": {str(k): v for k, v in users.items()},
        },
    )
    assert passed, {k: v for k, v in checks.items() if not v}


async def test_the_catalog_without_fully_define_sketch(
    scratch: Any, tmp_path: Path
) -> None:
    adapter = scratch.adapter
    tools = await _tools(adapter)
    spec = _spec_copy(tmp_path)

    built = _ok(
        await tools["build_weldment_profiles"](
            {
                "spec_file": str(spec),
                "root": str(tmp_path / "Weldment Profiles"),
                "define": "none",
            }
        )
    )
    results = json.loads(
        Path(built["profiles"]["results_file"]).read_text(encoding="utf-8")
    )
    rows = [
        {k: p[k] for k in ("path", "status", "seconds", "definition")}
        for p in results["profiles"]
    ]
    evidence("the catalog with define none", rows)
    checks = {
        "11 built": built["profiles"]["built"] == 11,
        "all under defined": all(r["definition"] == "under defined" for r in rows),
    }
    passed = all(checks.values())
    _record(
        adapter,
        "define_none",
        passed,
        {
            "define": "none",
            "checks": checks,
            "seconds_total": built["profiles"]["seconds"],
            "profiles": rows,
        },
    )
    assert passed, {k: v for k, v in checks.items() if not v}


def _insert_direct(adapter: Any, file: str, configuration: str) -> bool:
    """``InsertStructuralWeldment5`` itself on a new part's line: whether a member came.

    Tried twice, a second after the first, as the adapter does.
    """
    import time

    from solidworks_mcp.adapters import sw_type_info
    from solidworks_mcp.adapters.solidworks.com_utils import dispatch_array
    from solidworks_mcp.adapters.solidworks.weldment_profiles import _open_documents

    app = adapter.swApp
    before = {p.casefold() for _t, p in _open_documents(adapter) if p}
    model = app.NewPart()
    sw_type_info.flag_doc(model, 1)
    title = str(model.GetTitle())
    try:
        sketches = sw_type_info.flagged(model.SketchManager, "ISketchManager")
        model.ClearSelection2(True)
        sketches.AddToDB = True
        sketches.Insert3DSketch(True)
        line = sketches.CreateLine(0, 0, 0, 0, MEMBER_MM / 1000.0, 0)
        sketches.Insert3DSketch(True)
        sketches.AddToDB = False
        features = sw_type_info.flagged(model.FeatureManager, "IFeatureManager")
        features.InsertWeldmentFeature()
        group = sw_type_info.flagged(
            features.CreateStructuralMemberGroup(), "IStructuralMemberGroup"
        )
        group.Segments = dispatch_array([line])
        for _ in range(2):
            made = features.InsertStructuralWeldment5(
                file, 1, True, dispatch_array([group]), configuration
            )
            if made is not None:
                return True
            time.sleep(1.0)
        return False
    finally:
        app.CloseDoc(title)
        # SolidWorks opens the profile, unseen, to insert it.
        for _t, path in _open_documents(adapter):
            if path and path.casefold() not in before:
                app.CloseDoc(path)


async def test_a_profile_whose_configuration_has_another_name(
    scratch: Any, tmp_path: Path
) -> None:
    from solidworks_mcp.adapters import sw_type_info
    from solidworks_mcp.adapters.solidworks.com_utils import as_list
    from solidworks_mcp.adapters.solidworks.io import _byref_int

    adapter = scratch.adapter
    app = adapter.swApp
    tools = await _tools(adapter)
    flat = next(p for p in _catalog() if p["shape"] == "flat_bar")
    root = tmp_path / "Weldment Profiles"
    spec = _spec_copy(tmp_path, [flat])
    _ok(
        await tools["build_weldment_profiles"](
            {"spec_file": str(spec), "root": str(root), "define": "none"}
        )
    )
    file = str(root.joinpath(*flat["path"].split("/")))
    name = "Размер 40×4"  # a template in another language names it otherwise
    by_template_name = _insert_direct(adapter, file, "Default")
    errors, warnings = _byref_int(), _byref_int()
    model = app.OpenDoc6(file, 1, 1, "", errors, warnings)  # silent
    assert model is not None, errors.value
    sw_type_info.flag_doc(model, 1)
    try:
        configuration = sw_type_info.flagged(
            model.GetActiveConfiguration(), "IConfiguration"
        )
        was = str(configuration.Name)
        configuration.Name = name
        saved = bool(model.Save3(1, errors, warnings))  # silent
    finally:
        app.CloseDoc(str(model.GetPathName()))
    names = [str(n) for n in as_list(app.GetConfigurationNames(file))]
    by_new_name = _insert_direct(adapter, file, name)
    by_none = _insert_direct(adapter, file, "")

    area, cut_list = await _member(tools, adapter, file, "40x4", flat["area_mm2"])
    checks = {
        "renamed and saved": saved and names == [name],
        "SolidWorks refuses it under Default": was == "Default"
        and not by_template_name,
        "and under its new name": not by_new_name,
        "and takes it with none": by_none,
        "member area within 0.2 %": abs(_deviation(area, flat["area_mm2"]))
        <= TOLERANCE * 100,
        "cut list carries the properties": len(cut_list) == 1
        and all(cut_list[0].get(k) == v for k, v in flat["properties"].items()),
    }
    evidence("a renamed configuration", {"was": was, "now": names, **checks})
    passed = all(checks.values())
    _record(
        adapter,
        "renamed_configuration",
        passed,
        {
            "checks": checks,
            "configuration_was": was,
            "configurations_now": names,
            "member_volume_per_mm": round(area, 4),
            "area_mm2": flat["area_mm2"],
            "deviation_percent": _deviation(area, flat["area_mm2"]),
        },
    )
    assert passed, {k: v for k, v in checks.items() if not v}


def _pierce_at(adapter: Any, member: str, point: tuple[float, float]) -> bool:
    """Pierce the member at its profile's sketch point at ``point`` (mm, profile axes)."""
    import math

    from solidworks_mcp.adapters import sw_type_info
    from solidworks_mcp.adapters.solidworks.com_utils import as_list
    from solidworks_mcp.adapters.solidworks.features import _null_callout

    model = adapter.currentModel
    feature = _feature(sw_type_info.flagged(model, "IPartDoc").FeatureByName(member))
    assert feature is not None, f"no feature {member}"
    target = None
    sub = _feature(feature.GetFirstSubFeature())
    while sub is not None and target is None:
        if sub.GetTypeName2() == "ProfileFeature":
            sketch = sw_type_info.flagged(sub.GetSpecificFeature2(), "ISketch")
            for raw in as_list(sketch.GetSketchPoints2()):
                p = sw_type_info.flagged(raw, "ISketchPoint")
                if (
                    math.dist((float(p.X) * 1000, float(p.Y) * 1000), point)
                    <= AT_POINT_MM
                ):
                    target = p
                    break
        sub = _feature(sub.GetNextSubFeature())
    assert target is not None, f"no sketch point at {point} in {member}'s profile"
    data = sw_type_info.flagged(feature.GetDefinition(), "IStructuralMemberFeatureData")
    data.AccessSelections(model, _null_callout())
    data.LocateProfilePoint = target
    return bool(feature.ModifyDefinition(data, model, _null_callout()))


async def test_a_member_pierced_at_a_corner_point(scratch: Any, tmp_path: Path) -> None:
    adapter = scratch.adapter
    tools = await _tools(adapter)
    rhs = next(p for p in _catalog() if p["shape"] == "rect_tube")
    root = tmp_path / "Weldment Profiles"
    spec = _spec_copy(tmp_path, [rhs])
    _ok(
        await tools["build_weldment_profiles"](
            {"spec_file": str(spec), "root": str(root), "define": "none"}
        )
    )
    file = str(root.joinpath(*rhs["path"].split("/")))
    corner = (-30.0, -40.0)  # the box's bottom-left corner
    volume = {
        "volume_mm3": rhs["area_mm2"] * MEMBER_MM,
        "relative_tolerance": TOLERANCE,
    }
    _ok(await tools["create_part"]({"name": "pierce probe"}))
    try:
        _ok(
            await tools["create_structural_members"](
                {
                    "size": "80x60x2.7",
                    "profile_path": file,
                    "segments": [[0, 0, 0, 0, MEMBER_MM, 0]],
                    "name": "Probe",
                    "define": "none",
                }
            )
        )
        before = _ok(await tools["verify_model"](volume))["measured"]["bounding_box_mm"]
        modified = _pierce_at(adapter, "Probe", corner)
        after = _ok(await tools["verify_model"](volume))["measured"]["bounding_box_mm"]
    finally:
        _ok(await tools["close_model"]({"save": False}))

    def centre(box: dict[str, list[float]]) -> list[float]:
        return [
            round((lo + hi) / 2, 4)
            for lo, hi in zip(box["min"], box["max"], strict=True)
        ]

    moved = [
        round(b - a, 4) for a, b in zip(centre(before), centre(after), strict=True)
    ]
    on_line = [
        any(abs(v) <= AT_POINT_MM for v in (after["min"][k], after["max"][k]))
        for k in (0, 2)
    ]
    checks = {
        "modified": modified,
        "same size": [round(v, 4) for v in after["size"]]
        == [round(v, 4) for v in before["size"]],
        "moved 30 and 40 mm across the line": sorted(abs(moved[k]) for k in (0, 2))
        == [30.0, 40.0]
        and abs(moved[1]) <= AT_POINT_MM,
        "the corner runs along the line": all(on_line),
    }
    evidence(
        "a member pierced at its box's corner",
        {"before": before, "after": after, **checks},
    )
    passed = all(checks.values())
    _record(
        adapter,
        "pierce_point",
        passed,
        {
            "checks": checks,
            "pierce_point_mm": list(corner),
            "box_before_mm": before,
            "box_after_mm": after,
            "centre_moved_mm": moved,
        },
    )
    assert passed, {k: v for k, v in checks.items() if not v}
