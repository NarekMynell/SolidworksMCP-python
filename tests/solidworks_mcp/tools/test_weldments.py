"""Tests for the weldment tools, through the mock adapter."""

from __future__ import annotations

import json
from typing import Any

import pytest

from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
from solidworks_mcp.tools.weldments import (
    MAX_PLATES,
    CheckJointsInput,
    CreateEndCapsInput,
    CreatePlatesInput,
    CreateStructuralMembersInput,
    CreateTrimExtendInput,
    register_weldment_tools,
)


async def _tools(mcp_server: Any, adapter: Any) -> dict[str, Any]:
    await register_weldment_tools(mcp_server, adapter, {})
    return {t.name: t.fn for t in await mcp_server.list_tools()}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"size": "IPE300", "segments": [[0, 0, 0, 1, 0, 0]]},
            "standard and profile_type",
        ),
        (
            {"size": "IPE300", "standard": "DIN", "profile_type": "ipe"},
            "segments, segments_file or sketch",
        ),
        (
            {
                "size": "IPE300",
                "standard": "DIN",
                "profile_type": "ipe",
                "sketch": "S",
                "segments": [[0, 0, 0, 1, 0, 0]],
            },
            "segments, segments_file or sketch",
        ),
        (
            {"size": "A", "profile_path": "p.sldlfp", "segments": [[0, 0, 0, 1, 0]]},
            "isn't six numbers",
        ),
        (
            {"size": "A", "profile_path": "p.sldlfp", "segments": [[1, 1, 1, 1, 1, 1]]},
            "no length",
        ),
        (
            {
                "size": "A",
                "profile_path": "p.sldlfp",
                "segments": [[0, 0, 0, 1, 0, 0]],
                "segment_names": ["Line1"],
            },
            "segment_names needs sketch",
        ),
    ],
)
def test_member_input_refusals(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CreateStructuralMembersInput(**kwargs)


@pytest.mark.asyncio
async def test_a_frame_from_the_library_to_the_cut_list(
    mcp_server, mock_adapter
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_part()

    found = await tools["list_weldment_profiles"]({"query": "HEB400"})
    standards = await tools["list_weldment_profiles"]()
    types = await tools["list_weldment_profiles"]({"standard": "din"})
    sizes = await tools["list_weldment_profiles"](
        {"standard": "DIN", "profile_type": "IPE"}
    )
    unknown = await tools["list_weldment_profiles"]({"standard": "XX"})
    no_type = await tools["list_weldment_profiles"](
        {"standard": "DIN", "profile_type": "w"}
    )
    lonely = await tools["list_weldment_profiles"]({"profile_type": "ipe"})
    columns = await tools["create_structural_members"](
        {
            "standard": "DIN",
            "profile_type": "hea",
            "size": "HE400B",
            "segments": [[0, 0, 0, 0, 12000, 0], [9000, 0, 0, 9000, 12000, 0]],
            "name": "Columns",
            "sketch_name": "Column axes",
        }
    )
    again = await tools["create_structural_members"](
        {
            "standard": "DIN",
            "profile_type": "hea",
            "size": "HE400B",
            "sketch": "Column axes",
            "grouping": "each",
        }
    )
    bad = await tools["create_structural_members"](
        {
            "standard": "DIN",
            "profile_type": "hea",
            "size": "HE999B",
            "segments": [[0, 0, 0, 1, 0, 0]],
        }
    )
    plain = await tools["get_cut_list"]()
    await mock_adapter.set_material("AISI 1020")
    steel = await tools["get_cut_list"]({"export_path": "C:/shop/cut.csv"})

    assert found["profiles"]["matches"][0]["size"] == "HE400B"
    assert found["message"] == "1 profile size(s) match 'HEB400'"
    assert standards["message"].endswith("weldment profile standard(s)")
    assert types["message"] == "2 profile type(s) in DIN"
    assert sizes["message"] == "3 size(s) of ipe"
    assert unknown["status"] == "error" and no_type["status"] == "error"
    assert lonely["error_type"] == "invalid_input"
    assert columns["message"] == "Added 2 HE400B member(s) as Columns"
    assert columns["members"]["sketch"] == "Column axes"
    assert (again["members"]["groups"], again["members"]["grouping"]) == (2, "each")
    assert "No size 'HE999B'" in bad["message"]
    assert "default density" in plain["warning"]
    items = steel["cut_list"]["items"]
    assert [(i["quantity"], i["length_mm"]) for i in items] == [(4, 12000.0)]
    assert steel["cut_list"]["total_mass_kg"] == pytest.approx(4 * 12 * 155.0)
    assert steel["cut_list"]["export_path"] == "C:/shop/cut.csv"
    assert "default density" not in steel.get("warning", "")


@pytest.mark.asyncio
async def test_weldment_tools_need_a_part(mcp_server, mock_adapter) -> None:
    tools = await _tools(mcp_server, mock_adapter)

    nothing = await tools["get_cut_list"]({})
    await mock_adapter.create_assembly()
    members = await tools["create_structural_members"](
        {
            "profile_path": "C:/p/own.sldlfp",
            "size": "A",
            "segments": [[0, 0, 0, 1, 0, 0]],
        }
    )
    cut = await tools["get_cut_list"]({"export_path": "cut.txt"})

    assert nothing["status"] == "error"
    assert "needs an open part" in members["message"]
    assert cut["status"] == "error"


@pytest.mark.asyncio
async def test_the_mock_cut_list_needs_members_and_a_csv_name(
    mcp_server, mock_adapter
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_part()

    empty = await tools["get_cut_list"]()
    own = await tools["create_structural_members"](
        {
            "profile_path": "C:/p/own.sldlfp",
            "size": "A",
            "segments": [[0, 0, 0, 500, 0, 0]],
            "grouping": "single",
        }
    )
    missing = await tools["create_structural_members"](
        {"standard": "DIN", "profile_type": "ipe", "size": "IPE100", "sketch": "Nope"}
    )
    no_standard = await tools["create_structural_members"](
        {
            "standard": "XX",
            "profile_type": "ipe",
            "size": "IPE100",
            "segments": [[0, 0, 0, 1, 0, 0]],
        }
    )
    no_type = await tools["create_structural_members"](
        {
            "standard": "DIN",
            "profile_type": "w",
            "size": "IPE100",
            "segments": [[0, 0, 0, 1, 0, 0]],
        }
    )
    csv_only = await tools["get_cut_list"]({"export_path": "cut.txt"})

    assert "not a weldment" in empty["message"]
    assert own["members"]["profile"]["standard"] == "custom"
    assert "No sketch named 'Nope'" in missing["message"]
    assert "No weldment profile standard" in no_standard["message"]
    assert "No profile type" in no_type["message"]
    assert "must end in .csv" in csv_only["message"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"members": [], "boundary": ["Beams"]}, "at least 1 item"),
        (
            {"members": ["A", "B"], "boundary": ["C"], "corner_type": "miter"},
            "a miter corner joins one member to one boundary member",
        ),
        ({"members": ["A"], "boundary": ["C"], "weld_gap_mm": -1}, "greater than"),
    ],
)
def test_trim_input_refusals(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CreateTrimExtendInput(**kwargs)


@pytest.mark.asyncio
async def test_joists_trimmed_to_beams_shorten_in_the_cut_list(
    mcp_server, mock_adapter
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_part()
    for name, size, segments in (
        ("Beams", "IPE300", [[0, 0, z, 6000, 0, z] for z in (0, 4000)]),
        ("Joists", "IPE200", [[x, 0, 0, x, 0, 4000] for x in (1000, 2000)]),
    ):
        await tools["create_structural_members"](
            {
                "standard": "DIN",
                "profile_type": "ipe",
                "size": size,
                "segments": segments,
                "name": name,
            }
        )

    trimmed = await tools["create_trim_extend"](
        {"members": ["Joists"], "boundary": ["Beams"], "cut": "coped", "weld_gap_mm": 5}
    )
    unknown = await tools["create_trim_extend"](
        {"members": ["Purlins"], "boundary": ["Beams"]}
    )
    both = await tools["create_trim_extend"](
        {"members": ["Joists"], "boundary": ["Joists"]}
    )
    corner = await tools["create_trim_extend"](
        {"members": ["Joists"], "boundary": ["Beams[1]"], "corner_type": "butt1"}
    )
    cut = await tools["get_cut_list"]()

    assert trimmed["message"] == (
        "Trimmed 2 member body(ies) to 2 boundary body(ies) as Trim/Extend1"
    )
    assert [m["now"] for m in trimmed["trim"]["members"]] == [
        "Trim/Extend1[1]",
        "Trim/Extend1[2]",
    ]
    assert "warning" not in trimmed
    assert "No structural member or body named 'Purlins'" in unknown["message"]
    assert "can't be both trimmed and the boundary" in both["message"]
    assert "A butt1 corner joins two members" in corner["message"]
    lengths = {i["description"]: i["length_mm"] for i in cut["cut_list"]["items"]}
    assert lengths == {"IPE300": 6000.0, "IPE200": 3995.0}


@pytest.mark.asyncio
async def test_a_trim_warning_is_passed_on(
    mcp_server, mock_adapter, monkeypatch
) -> None:
    async def partly(_params: dict[str, Any]) -> AdapterResult[dict[str, Any]]:
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "feature": "Trim/Extend1",
                "members": [{"name": "J[2]", "now": "J[2]", "changed": False}],
                "boundary": ["B[1]"],
                "unchanged": ["J[2]"],
                "warning": "1 member(s) didn't change: J[2]",
            },
        )

    monkeypatch.setattr(mock_adapter, "create_trim_extend", partly)
    tools = await _tools(mcp_server, mock_adapter)

    reply = await tools["create_trim_extend"](
        {"members": ["J[2]"], "boundary": ["B[1]"]}
    )

    assert reply["warning"] == "1 member(s) didn't change: J[2]"
    assert "warning" not in reply["trim"]


_FRAME = {"standard": "DIN", "profile_type": "ipe", "size": "IPE200"}


@pytest.mark.asyncio
async def test_segments_from_a_file(mcp_server, mock_adapter, tmp_path) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_part()
    listed = tmp_path / "chords.json"
    listed.write_text(
        "[[0, 0, 0, 0, 3000, 0], [1000, 0, 0, 1000, 3000, 0]]", encoding="utf-8"
    )
    wrapped = tmp_path / "braces.json"
    wrapped.write_text('{"segments": [[0, 0, 0, 1000, 1000, 0]]}', encoding="utf-8")
    texts = {
        "not_json": "[[0, 0,",
        "empty": "[]",
        "short": "[[0, 0, 0, 1]]",
        "zero": "[[1, 1, 1, 1, 1, 1]]",
        "words": '[[0, 0, 0, "a", 0, 0]]',
        "many": "[" + ",".join(["[0,0,0,1,0,0]"] * 2001) + "]",
    }
    paths = {"missing": str(tmp_path / "nope.json")}
    for name, text in texts.items():
        path = tmp_path / f"{name}.json"
        path.write_text(text, encoding="utf-8")
        paths[name] = str(path)

    chords = await tools["create_structural_members"](
        {**_FRAME, "segments_file": str(listed)}
    )
    braces = await tools["create_structural_members"](
        {**_FRAME, "segments_file": str(wrapped)}
    )
    errors = {
        name: await tools["create_structural_members"](
            {**_FRAME, "segments_file": path}
        )
        for name, path in paths.items()
    }

    assert chords["members"]["segments"] == 2 and braces["members"]["segments"] == 1
    assert "doesn't exist" in errors["missing"]["message"]
    assert "isn't readable JSON" in errors["not_json"]["message"]
    assert "must hold a list" in errors["empty"]["message"]
    assert "isn't six numbers" in errors["short"]["message"]
    assert "has no length" in errors["zero"]["message"]
    assert "isn't a number" in errors["words"]["message"]
    assert "split it" in errors["many"]["message"]
    assert all(e["error_type"] == "invalid_input" for e in errors.values())


@pytest.mark.parametrize(
    ("model", "kwargs", "message"),
    [
        (CreateEndCapsInput, {}, "give ends"),
        (CreateEndCapsInput, {"ends": [[0, 0, 0]], "open_ends": True}, "give ends"),
        (CreateEndCapsInput, {"ends": [[0, 0]]}, r"end 0 isn't \[x, y, z\]"),
        (CreateEndCapsInput, {"open_ends": True, "thickness_mm": 0}, "greater than 0"),
        (CheckJointsInput, {"contact_mm": 0}, "greater than 0"),
        (CheckJointsInput, {"max_listed": 0}, "greater than or equal to 1"),
    ],
)
def test_joint_and_cap_input_refusals(
    model: Any, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        model(**kwargs)


@pytest.mark.asyncio
async def test_a_portal_frame_checked_trimmed_and_capped(
    mcp_server, mock_adapter
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_part()
    await tools["create_structural_members"](
        {
            **_FRAME,
            "name": "Posts",
            "grouping": "each",
            "corner_treatment": "none",
            "segments": [[0, 0, 0, 0, 3000, 0], [6000, 0, 0, 6000, 3000, 0]],
        }
    )
    await tools["create_structural_members"](
        {**_FRAME, "name": "Beam", "segments": [[0, 3000, 0, 6000, 3000, 0]]}
    )
    await tools["create_structural_members"](
        {**_FRAME, "name": "Stray", "segments": [[0, 0, 9000, 1000, 0, 9000]]}
    )

    raw = await tools["check_joints"]()
    posts_only = await tools["check_joints"]({"members": ["Posts"]})
    unknown = await tools["check_joints"]({"members": ["Purlins"]})
    await tools["create_trim_extend"]({"members": ["Posts"], "boundary": ["Beam"]})
    trimmed = await tools["check_joints"]({"max_listed": 1})
    capped = await tools["create_end_caps"](
        {"open_ends": True, "thickness_mm": 6, "name": "Caps"}
    )
    missed = await tools["create_end_caps"]({"ends": [[50000, 0, 0]]})
    done = await tools["check_joints"]()
    none_left = await tools["create_end_caps"]({"open_ends": True})
    at_point = await tools["create_end_caps"]({"ends": [[1000, 0, 9000]]})

    assert raw["joints"]["overlaps"]["count"] == 2  # each post runs into the beam's end
    assert raw["joints"]["loose"] == ["Stray[1]"] and raw["joints"]["groups"] == 2
    assert raw["message"].startswith(
        "2 overlapping pair(s), 4 open end(s), 1 loose body(ies)"
    )
    assert posts_only["joints"]["checked_bodies"] == 2
    assert "No structural member or body named 'Purlins'" in unknown["message"]
    assert trimmed["joints"]["overlaps"]["count"] == 0
    assert len(trimmed["joints"]["open_ends"]["ends"]) == 1
    assert (
        capped["message"] == "Capped 4 end(s) as Caps"
        and capped["caps"]["placement"] == "inward"
    )
    assert "No member end within 20 mm" in missed["message"]
    assert done["joints"]["open_ends"]["count"] == 0
    assert "No open member end to cap" in none_left["message"]
    assert at_point["caps"]["caps"] == 1


@pytest.mark.asyncio
async def test_joint_tools_need_a_part_with_bodies(mcp_server, mock_adapter) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_part()
    empty = await tools["check_joints"]()
    await mock_adapter.create_assembly()
    joints = await tools["check_joints"]()
    caps = await tools["create_end_caps"]({"open_ends": True})

    assert "no bodies to check" in empty["message"]
    assert "check_joints needs an open part" in joints["message"]
    assert "create_end_caps needs an open part" in caps["message"]


@pytest.mark.asyncio
async def test_a_one_member_part_is_never_loose(mcp_server, mock_adapter) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_part()
    await tools["create_structural_members"](
        {**_FRAME, "segments": [[0, 0, 0, 0, 1000, 0]]}
    )

    result = await tools["check_joints"]()

    assert result["joints"]["loose"] == [] and result["joints"]["clean"] is True
    assert result["message"].endswith("; clean")


@pytest.mark.asyncio
async def test_an_end_cap_warning_is_passed_on(
    mcp_server, mock_adapter, monkeypatch
) -> None:
    async def fewer(_params: dict[str, Any]) -> AdapterResult[dict[str, Any]]:
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "feature": "End cap1",
                "caps": 1,
                "warning": "2 end(s) selected but 1 cap body(ies) made",
            },
        )

    monkeypatch.setattr(mock_adapter, "create_end_caps", fewer)
    tools = await _tools(mcp_server, mock_adapter)

    reply = await tools["create_end_caps"]({"open_ends": True})

    assert reply["warning"].startswith("2 end(s) selected")
    assert "warning" not in reply["caps"]


_PLATE = {
    "name": "Base plate",
    "mark": "BP-1",
    "thickness": 15,
    "width": 250,
    "height": 250,
    "origin": [0, 0, 0],
    "normal": [0, -1, 0],
    "holes": [{"diameter": 22, "positions": [[-85, -85], [85, 85]]}],
}


@pytest.mark.parametrize(
    ("plate", "message"),
    [
        (
            {**_PLATE, "outline": [[0, 0], [10, 0], [0, 10]]},
            "outline, or width and height",
        ),
        (
            {k: v for k, v in _PLATE.items() if k != "height"},
            "outline, or width and height",
        ),
        (
            {
                **_PLATE,
                "width": None,
                "height": None,
                "outline": [[0, 0], [10, 0, 1], [0, 10]],
            },
            "each outline corner is",
        ),
        (
            {**_PLATE, "holes": [{"diameter": 22, "positions": [[0, 0, 0]]}]},
            "each hole position is",
        ),
        (
            {**_PLATE, "holes": [{"diameter": 22, "positions": [[200, 0]]}]},
            "outside the plate",
        ),
        ({**_PLATE, "normal": [0, 0, 0]}, "normal has no length"),
        ({**_PLATE, "colour": "red"}, "Extra inputs"),
    ],
)
def test_plate_input_refusals(plate: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CreatePlatesInput(plates=[plate])


def test_plates_or_a_file_not_both() -> None:
    with pytest.raises(ValueError, match="plates or plates_file"):
        CreatePlatesInput()
    with pytest.raises(ValueError, match="plates or plates_file"):
        CreatePlatesInput(plates=[_PLATE], plates_file="p.json")


@pytest.mark.asyncio
async def test_plates_with_holes_then_their_dxfs(
    mcp_server, mock_adapter, tmp_path
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_part()
    second = {**_PLATE, "mark": "BP-2", "origin": [4000, 0, 0]}
    cap = {
        **_PLATE,
        "mark": "CP-1",
        "name": "Cap",
        "origin": [0, 3040, 0],
        "normal": [0, 1, 0],
        "holes": [],
    }
    gusset = {
        "name": "Gusset",
        "mark": "GP-1",
        "thickness": 10,
        "outline": [[-100, -60], [100, -60], [100, 60], [-60, 60]],
        "origin": [2000, 1500, 0],
        "normal": [1, 1, 0],
    }
    made = await tools["create_plates"]({"plates": [_PLATE, second, cap, gusset]})
    assert made["status"] == "success", made
    assert made["message"] == "Added 4 plate(s), 4 hole(s)"
    data = made["plates"]
    assert [p["plane"] for p in data["plates"]] == [
        "Top Plane",
        "Top Plane",
        "Plates Y+3040",
        "GP-1 Gusset plane",
    ]
    assert data["planes_made"] == 2
    assert data["folder"]["folder"] == "Plates"

    unsaved = await tools["export_plate_dxfs"]({"output_dir": str(tmp_path)})
    assert unsaved["status"] == "error"
    assert "never been saved" in unsaved["message"]

    # A part on disk: the same plates, opened from its file.
    await mock_adapter.open_model(str(tmp_path / "frame.sldprt"))
    await tools["create_plates"]({"plates": [_PLATE, second, cap, gusset]})
    dxfs = await tools["export_plate_dxfs"](
        {"output_dir": str(tmp_path / "dxf"), "prefix": "J1_"}
    )
    assert dxfs["status"] == "success", dxfs
    names = sorted(p.name for p in (tmp_path / "dxf").iterdir())
    assert names == [
        "J1_BP-1_t15_x2.dxf",
        "J1_CP-1_t15_x1.dxf",
        "J1_GP-1_t10_x1.dxf",
        "J1_plates.csv",
    ]
    assert dxfs["message"] == "Wrote 3 plate DXF(s), 4 plate(s) to cut"


@pytest.mark.asyncio
async def test_plates_from_a_file(mcp_server, mock_adapter, tmp_path) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_part()
    good = tmp_path / "plates.json"
    good.write_text(json.dumps({"plates": [_PLATE]}), encoding="utf-8")
    made = await tools["create_plates"]({"plates_file": str(good), "define": "none"})
    assert made["status"] == "success", made
    assert made["plates"]["plates"][0]["definition"] == "not checked"

    cases = {
        "missing.json": (None, "doesn't exist"),
        "broken.json": ("{", "isn't readable JSON"),
        "shape.json": ('{"plates": 3}', "must hold a list of plates"),
        "many.json": (json.dumps([_PLATE] * (MAX_PLATES + 1)), "split it"),
        "entry.json": (
            json.dumps([_PLATE, {**_PLATE, "thickness": -1}]),
            "entry 1 thickness",
        ),
    }
    for name, (text, message) in cases.items():
        path = tmp_path / name
        if text is not None:
            path.write_text(text, encoding="utf-8")
        reply = await tools["create_plates"]({"plates_file": str(path)})
        assert reply["status"] == "error", name
        assert message in reply["message"], (name, reply["message"])


@pytest.mark.asyncio
async def test_a_plate_the_adapter_refuses_is_an_error(
    mcp_server, mock_adapter, monkeypatch
) -> None:
    async def refuse(_params: dict[str, Any]) -> AdapterResult[dict[str, Any]]:
        return AdapterResult(
            status=AdapterResultStatus.ERROR, error="SolidWorks refused to extrude BP-1"
        )

    monkeypatch.setattr(mock_adapter, "create_plates", refuse)
    tools = await _tools(mcp_server, mock_adapter)
    reply = await tools["create_plates"]({"plates": [_PLATE]})
    assert reply["status"] == "error"
    assert "refused to extrude" in reply["message"]


@pytest.mark.asyncio
async def test_a_plate_left_loose_is_flagged(
    mcp_server, mock_adapter, monkeypatch
) -> None:
    async def loose(_params: dict[str, Any]) -> AdapterResult[dict[str, Any]]:
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={
                "plates": [
                    {
                        "name": "BP-1 Base plate",
                        "holes": 4,
                        "definition": "under defined",
                    }
                ]
            },
        )

    monkeypatch.setattr(mock_adapter, "create_plates", loose)
    tools = await _tools(mcp_server, mock_adapter)
    reply = await tools["create_plates"]({"plates": [_PLATE]})
    assert reply["warning"] == "Not fully defined: BP-1 Base plate"


@pytest.mark.asyncio
async def test_plates_need_a_part(mcp_server, mock_adapter, tmp_path) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_assembly()
    assert (await tools["create_plates"]({"plates": [_PLATE]}))["status"] == "error"
    assert (await tools["export_plate_dxfs"]({"output_dir": str(tmp_path)}))[
        "status"
    ] == "error"
