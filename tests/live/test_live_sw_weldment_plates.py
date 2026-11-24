"""Plates, a tidy tree and plate DXFs (steel-structures toolkit), checked on real SolidWorks.

A 4 x 3 m frame of SHS 80x80x5 on four base plates, as a CAD specialist
would want to open it:

1. ``create_structural_members`` with ``name`` and ``folder``: each skeleton
   comes out fully defined (every node dimensioned to the main planes) and
   in the "Frame" folder with its members; a brace added later joins the
   folder too.
2. ``create_plates``: four 250 x 250 x 15 base plates with four Ø22 holes,
   level, on the Top plane, and a tilted gusset with two Ø14 holes; every
   plate sketch fully defined, each plate its own body, named and described.
3. ``set_feature_folders`` renames a feature; ``check_feature_tree`` finds
   nothing to fix.
4. ``export_plate_dxfs``: one DXF for the four base plates (holes cut, the
   area 250² less four Ø22 holes) and one for the gusset, and plates.csv.

Collected only with ``SOLIDWORKS_MCP_RUN_REAL_INTEGRATION=1`` on Windows.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

_SHS = {"standard": "iso", "profile_type": "square tube", "size": "80 x 80 x 5"}
_HOLES = [{"diameter": 22, "positions": [[-85, -85], [85, -85], [85, 85], [-85, 85]]}]


async def _tools(adapter: Any) -> dict[str, Any]:
    """Every tool, registered against ``adapter``: name -> function."""
    from fastmcp import FastMCP

    from solidworks_mcp.tools import register_tools

    mcp = FastMCP("plates-live")
    await register_tools(mcp, adapter, {})
    return {t.name: t.fn for t in await mcp.list_tools()}


def _volume(reply: dict[str, Any]) -> float:
    """The volume a get_mass_properties reply gives, mm³."""
    volume = reply["mass_properties"]["volume"]
    return float(volume["value"] if isinstance(volume, dict) else volume)


def _ok(reply: dict[str, Any]) -> dict[str, Any]:
    assert reply["status"] == "success", reply
    return reply


async def test_a_frame_on_base_plates_tidy_and_cut(
    scratch: Any, tmp_path: Path
) -> None:
    tools = await _tools(scratch.adapter)
    _ok(await tools["create_part"]({"name": "plates_frame"}))

    # 1. Members, skeletons fully defined, in one folder.
    columns = _ok(
        await tools["create_structural_members"](
            {
                **_SHS,
                "segments": [
                    [x, 0, z, x, 3000, z] for x in (0, 4000) for z in (0, 3000)
                ],
                "grouping": "each",
                "name": "Columns",
                "folder": "Frame",
            }
        )
    )["members"]
    assert columns["sketch"] == "Columns skeleton"
    assert columns["skeleton"]["definition"] == "fully defined"
    assert columns["skeleton"]["fixed"] == 0
    assert columns["folder"]["moved"] == ["Columns skeleton", "Columns"]
    beams = _ok(
        await tools["create_structural_members"](
            {
                **_SHS,
                "segments": [
                    [0, 3000, 0, 4000, 3000, 0],
                    [4000, 3000, 0, 4000, 3000, 3000],
                    [4000, 3000, 3000, 0, 3000, 3000],
                    [0, 3000, 3000, 0, 3000, 0],
                ],
                "name": "Beams",
                "folder": "Frame",
            }
        )
    )["members"]
    assert beams["skeleton"]["definition"] == "fully defined"
    assert beams["folder"]["refused"] == []

    # 2. Plates: four level base plates, one tilted gusset.
    base = [
        {
            "name": "Base plate",
            "mark": f"BP-{i + 1}",
            "thickness": 15,
            "width": 250,
            "height": 250,
            "origin": [x, 0, z],
            "normal": [0, -1, 0],
            "holes": _HOLES,
        }
        for i, (x, z) in enumerate([(0, 0), (4000, 0), (0, 3000), (4000, 3000)])
    ]
    gusset = {
        "name": "Gusset",
        "mark": "GP-1",
        "thickness": 10,
        "outline": [[-100, -60], [100, -60], [100, 60], [-60, 60]],
        "origin": [2000, 1500, 0],
        "normal": [1, 1, 0],
        "holes": [{"diameter": 14, "positions": [[0, 0], [60, 0]]}],
    }
    plates = _ok(await tools["create_plates"]({"plates": [*base, gusset]}))["plates"]
    assert [p["definition"] for p in plates["plates"]] == ["fully defined"] * 5
    assert [p["plane"] for p in plates["plates"][:4]] == ["Top Plane"] * 4
    assert plates["plates"][0]["description"] == "PL15 250x250"
    assert plates["folder"]["refused"] == []
    assert plates["bodies_after"] == 13

    # A brace drawn after the plates still lands in the members' folder.
    brace = _ok(
        await tools["create_structural_members"](
            {
                **_SHS,
                "segments": [[0, 0, 0, 4000, 3000, 0]],
                "name": "Brace",
                "folder": "Frame",
            }
        )
    )["members"]
    assert brace["folder"]["moved"] == ["Brace skeleton", "Brace"]
    assert brace["skeleton"]["definition"] == "fully defined"

    # 3. Names and the audit.
    renamed = _ok(
        await tools["set_feature_folders"]({"rename": {"Plate axes": "Gusset axis"}})
    )["tree"]
    assert renamed["renamed"] == {"Plate axes": "Gusset axis"}
    audit = _ok(await tools["check_feature_tree"]({}))["audit"]
    assert audit["verdict"] == "clean", audit
    assert audit["not_fully_defined"] == []
    assert audit["outside_folders"] == 0

    # 4. Saved, then one DXF per distinct plate.
    _ok(await tools["save_as"]({"file_path": str(tmp_path / "plates_frame.SLDPRT")}))
    dxfs = _ok(await tools["export_plate_dxfs"]({"output_dir": str(tmp_path / "dxf")}))[
        "dxfs"
    ]
    by_mark = {f["mark"]: f for f in dxfs["files"]}
    assert set(by_mark) == {"BP-1", "GP-1"}
    assert by_mark["BP-1"]["quantity"] == 4
    assert by_mark["BP-1"]["thickness_mm"] == 15.0
    assert math.isclose(
        by_mark["BP-1"]["area_mm2"], 250 * 250 - 4 * math.pi * 11**2, abs_tol=1.0
    )
    assert math.isclose(
        by_mark["GP-1"]["area_mm2"], 21600 - 2 * math.pi * 7**2, abs_tol=1.0
    )
    for file in dxfs["files"]:
        assert Path(file["file"]).stat().st_size > 1000
    with open(dxfs["csv"], encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 2


async def test_the_server_stack_forwards_the_plate_and_tree_tools() -> None:
    """Through the adapter the server builds (load_config + create_adapter): an
    agent's run once got "create_plates is not implemented" there while the
    direct adapter worked."""
    from solidworks_mcp import adapters
    from solidworks_mcp.config import load_config

    adapter = await adapters.create_adapter(load_config())
    await adapter.connect()
    try:
        tools = await _tools(adapter)
        _ok(await tools["create_part"]({"name": "stack_plates"}))
        try:
            plate = {
                "name": "Base plate",
                "mark": "BP-9",
                "thickness": 20,
                "width": 300,
                "height": 300,
                "origin": [0, 0, 0],
                "normal": [0, 1, 0],
                "holes": [{"diameter": 18, "positions": [[-115, -115], [115, 115]]}],
            }
            made = _ok(await tools["create_plates"]({"plates": [plate]}))
            assert made["plates"]["plates"][0]["definition"] == "fully defined"
            audit = _ok(await tools["check_feature_tree"]({}))["audit"]
            assert audit["verdict"] == "clean"
            _ok(
                await tools["set_feature_folders"](
                    {"rename": {"BP-9 Base plate": "BP-9 Foot"}}
                )
            )
        finally:
            await tools["close_model"]({"save": False})
    finally:
        await adapter.disconnect()


async def test_holes_through_a_joist_and_its_fin_plate_and_a_cope(
    scratch: Any,
) -> None:
    """An IPE160 joist along z (its web the x = 0 plane) with an 8 mm fin
    plate on its web: two Ø18 holes go through web and plate in one cut, a
    cope takes the top flange off the last 90 mm; each sketch fully defined,
    each cut through the bodies it should reach and no other, and the plate
    still described in the cut list after its holes were cut."""
    tools = await _tools(scratch.adapter)
    _ok(await tools["create_part"]({"name": "cuts"}))
    _ok(
        await tools["create_structural_members"](
            {
                "standard": "DIN",
                "profile_type": "ipe",
                "size": "IPE160",
                "segments": [[0, 0, 0, 0, 0, 2000]],
                "name": "Joist",
                "folder": "Frame",
            }
        )
    )
    _ok(
        await tools["create_plates"](
            {
                "plates": [
                    {
                        "name": "Fin plate",
                        "mark": "FP-1",
                        "thickness": 8,
                        "width": 100,
                        "height": 95,
                        "origin": [2.5, -10, 1950],
                        "normal": [1, 0, 0],
                    }
                ]
            }
        )
    )
    before = _volume(_ok(await tools["get_mass_properties"]({})))
    holes = {
        "name": "FJ-1 web holes",
        "origin": [4, -10, 1950],
        "normal": [1, 0, 0],
        "holes": [{"diameter": 18, "positions": [[0, -27.5], [0, 27.5]]}],
        "depth": 30,
    }
    cope = {
        "name": "FJ-1 end cope",
        "origin": [0, 80, 2000],
        "normal": [1, 0, 0],
        "outline": [[-10, -30], [90, -30], [90, 10], [-10, 10]],
        "depth": 100,
    }
    made = _ok(await tools["create_cuts"]({"cuts": [holes, cope]}))["cuts"]
    by_name = {c["name"]: c for c in made["cuts"]}
    assert [c["definition"] for c in made["cuts"]] == ["fully defined"] * 2
    assert len(by_name["FJ-1 web holes"]["bodies"]) == 2, by_name
    assert len(by_name["FJ-1 end cope"]["bodies"]) == 1, by_name
    assert made["folder"]["refused"] == []
    removed = before - _volume(_ok(await tools["get_mass_properties"]({})))
    holes_mm3 = 2 * math.pi * 9**2 * (5.0 + 8.0)  # web 5 mm, plate 8 mm
    flange_mm3 = 82 * 7.4 * 90  # the top flange over the cope's length
    assert holes_mm3 + flange_mm3 < removed < holes_mm3 + flange_mm3 + 25_000, removed
    items = _ok(await tools["get_cut_list"]({}))["cut_list"]["items"]
    assert "PL8 100x95" in {i["description"] for i in items}, items
    audit = _ok(await tools["check_feature_tree"]({}))["audit"]
    assert audit["verdict"] == "clean", audit

    missed = await tools["create_cuts"](
        {
            "cuts": [
                {
                    "name": "In the air",
                    "origin": [500, 500, 500],
                    "normal": [1, 0, 0],
                    "holes": [{"diameter": 18, "positions": [[0, 0]]}],
                    "depth": 10,
                }
            ]
        }
    )
    assert missed["status"] == "error"
    assert "no steel within 10 mm" in missed["message"]


async def test_vent_and_drain_holes_through_one_wall(scratch: Any) -> None:
    """A post and a rail of SHS 80x80x5, hung as built: each gets a vent near
    its higher end through the wall facing up (or, plumb, one side) and a drain
    near its lower end through the opposite wall, one wall thick each."""
    tools = await _tools(scratch.adapter)
    _ok(await tools["create_part"]({"name": "vents"}))
    _ok(
        await tools["create_structural_members"](
            {
                **_SHS,
                "segments": [[0, 0, 0, 0, 2000, 0], [500, 1000, 0, 2500, 1000, 0]],
                "grouping": "each",
                "name": "Tubes",
                "folder": "Frame",
            }
        )
    )
    before = _volume(_ok(await tools["get_mass_properties"]({})))
    vents = _ok(
        await tools["create_vent_holes"]({"members": ["Tubes"], "diameter": 12})
    )["vents"]
    assert [h["kind"] for h in vents["holes"]] == ["vent", "drain", "vent", "drain"]
    assert vents["skipped"] == []
    after = _volume(_ok(await tools["get_mass_properties"]({})))
    one_hole = math.pi * 6**2 * 5  # Ø12 through a 5 mm wall
    removed = before - after
    assert 3.5 * one_hole < removed < 4.5 * one_hole, removed
    rail = [h for h in vents["holes"] if h["at_mm"][1] != 0 and h["at_mm"][0] > 400]
    assert {round(h["at_mm"][1]) for h in rail} == {1040, 960}
    audit = _ok(await tools["check_feature_tree"]({}))["audit"]
    assert audit["not_fully_defined"] == []
