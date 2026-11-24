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
