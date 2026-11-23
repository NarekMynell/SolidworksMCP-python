"""Tests for the weldment tools, through the mock adapter."""

from __future__ import annotations

from typing import Any

import pytest

from solidworks_mcp.tools.weldments import (
    CreateStructuralMembersInput,
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
            "segments or sketch",
        ),
        (
            {
                "size": "IPE300",
                "standard": "DIN",
                "profile_type": "ipe",
                "sketch": "S",
                "segments": [[0, 0, 0, 1, 0, 0]],
            },
            "segments or sketch",
        ),
        (
            {"size": "A", "profile_path": "p.sldlfp", "segments": [[0, 0, 0, 1, 0]]},
            "has 5 numbers",
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
