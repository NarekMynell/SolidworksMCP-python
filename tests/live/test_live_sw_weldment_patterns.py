"""Repetition, facing and closed ends (steel-structures toolkit), checked on real SolidWorks.

1. ``create_structural_members`` points every profile's height ``up``: a
   rafter sloping 17.7 degrees in the xy plane, which SolidWorks' own frame
   rolls by its slope, comes out upright (``max_roll_deg`` 0, its 100 mm
   side square to the frame plane); purlins turn square to the roof.
2. ``pattern_members``: one baluster trimmed to two rails, copied 25 times
   less two at a gate: 23 balusters, every one at the trimmed length; a
   frame with its base plate copied along z; a mirrored brace.
3. ``check_joints``: a post whose top only a narrow bar covers is partly
   open; ``create_end_caps`` finds the post's foot from a point in its bore
   and describes the cap in the cut list.
4. ``create_trim_extend``: beams drawn round columns (auto grouping) stop at
   the columns; the twin bodies SolidWorks' trim returns for some of them are
   deleted, one body per member is left, nothing overlaps.

Collected only with ``SOLIDWORKS_MCP_RUN_REAL_INTEGRATION=1`` on Windows.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

_RHS = {"standard": "BSI", "profile_type": "rhs", "size": "RHS150x100x6.3"}
_SHS40 = {"standard": "BSI", "profile_type": "shs", "size": "RHS40x40x3.2"}
_SHS100 = {"standard": "BSI", "profile_type": "shs", "size": "RHS100x100x4"}


async def _tools(adapter: Any) -> dict[str, Any]:
    """Every tool, registered against ``adapter``: name -> function."""
    from fastmcp import FastMCP

    from solidworks_mcp.tools import register_tools

    mcp = FastMCP("patterns-live")
    await register_tools(mcp, adapter, {})
    return {t.name: t.fn for t in await mcp.list_tools()}


def _ok(reply: dict[str, Any]) -> dict[str, Any]:
    assert reply["status"] == "success", reply
    return reply


def _box(adapter: Any, name: str) -> list[float]:
    """A body's box, mm."""
    from solidworks_mcp.adapters import sw_type_info as t

    part = t.flagged(adapter.currentModel, "IPartDoc")
    for raw in part.GetBodies2(0, True):
        body = t.flagged(raw, "IBody2")
        if str(body.Name) == name:
            return [round(v * 1000.0, 1) for v in body.GetBodyBox()]
    raise AssertionError(f"no body {name}")


async def test_facing_patterns_and_closed_ends(scratch: Any) -> None:
    tools = await _tools(scratch.adapter)
    _ok(await tools["create_part"]({"name": "patterns"}))

    # 1. A sloped rafter stands upright; purlins square to the roof.
    rafter = _ok(
        await tools["create_structural_members"](
            {
                **_RHS,
                "segments": [[295, 4237, 0, 5805, 2475, 0]],
                "name": "Rafter",
                "define": "none",
            }
        )
    )
    assert rafter["members"]["orientation"]["max_roll_deg"] < 0.5
    box = _box(scratch.adapter, "Rafter")
    assert box[5] - box[2] == pytest.approx(100.0, abs=0.5)  # not rolled out of plane
    purlins = _ok(
        await tools["create_structural_members"](
            {
                "standard": "BSI",
                "profile_type": "rhs",
                "size": "RHS100x50x4",
                "segments": [[1000, 4000, 200, 1000, 4000, 2800]],
                "up": [0.3045, 0.9525, 0.0],
                "name": "Purlin",
                "define": "none",
            }
        )
    )
    assert purlins["members"]["orientation"]["max_roll_deg"] < 0.5

    # 2. One baluster, trimmed, copied with a gate gap.
    _ok(
        await tools["create_structural_members"](
            {
                **_SHS40,
                "segments": [
                    [8000, 100, 0, 11000, 100, 0],
                    [8000, 1000, 0, 11000, 1000, 0],
                ],
                "name": "Rails",
                "folder": "Railing",
                "grouping": "each",
            }
        )
    )
    _ok(
        await tools["create_structural_members"](
            {
                **_SHS40,
                "segments": [[8100, 100, 0, 8100, 1000, 0]],
                "name": "Baluster",
                "folder": "Railing",
            }
        )
    )
    _ok(
        await tools["create_trim_extend"](
            {
                "members": ["Baluster"],
                "boundary": ["Rails"],
                "name": "Baluster to rails",
            }
        )
    )
    balusters = _ok(
        await tools["pattern_members"](
            {
                "members": ["Baluster"],
                "along": {"direction": [1, 0, 0], "spacing": 120, "count": 25},
                "skip": [10, 11],
                "name": "Balusters",
            }
        )
    )["pattern"]
    assert (balusters["instances"], balusters["bodies_added"]) == (23, 22)
    cut = _ok(await tools["get_cut_list"]({}))["cut_list"]["items"]
    lengths = {
        i["length_mm"]: i["quantity"] for i in cut if i["description"] == "RHS40x40x3.2"
    }
    assert lengths.get(860.0) == 23  # every copy trimmed like the original
    copies = _ok(await tools["check_joints"]({"members": ["Balusters"]}))["joints"]
    assert copies["checked_bodies"] == 22  # a pattern's name stands for its copies
    assert copies["overlaps"]["count"] == 0 and copies["loose"] == []

    mirror = _ok(
        await tools["pattern_members"](
            {
                "members": ["Rafter"],
                "mirror": {"point": [0, 0, 1500], "normal": [0, 0, 1]},
            }
        )
    )["pattern"]
    assert mirror["bodies_added"] == 1

    # 3. A post half covered is partly open; a cap from a point in the bore.
    _ok(
        await tools["create_structural_members"](
            {
                **_SHS100,
                "segments": [[20000, 0, 0, 20000, 1000, 0]],
                "name": "Post",
                "define": "none",
            }
        )
    )
    _ok(
        await tools["create_structural_members"](
            {
                **_SHS40,
                "segments": [[19500, 1020, -30, 20500, 1020, -30]],
                "name": "Bar",
                "define": "none",
            }
        )
    )
    joints = _ok(await tools["check_joints"]({"members": ["Post"]}))["joints"]
    assert any(e["body"] == "Post" for e in joints["partly_open_ends"]["ends"])
    caps = _ok(await tools["create_end_caps"]({"ends": [[20000, 0, 0]]}))["caps"]
    assert caps["caps"] == 1
    assert caps["descriptions"] and caps["descriptions"][0].startswith(
        "End cap PL5 100"
    )

    # 4. Beams round columns stop at them; the trim leaves nothing extra.
    columns = [[x, 0, z, x, 3000, z] for x in (30000, 32480, 34960) for z in (0, 3000)]
    _ok(
        await tools["create_structural_members"](
            {
                **_SHS100,
                "segments": columns,
                "name": "Columns",
                "define": "none",
                "grouping": "each",
            }
        )
    )
    floor = [
        [30000 + 2480 * i, 1500, z, 32480 + 2480 * i, 1500, z]
        for i in range(2)
        for z in (0, 3000)
    ]
    floor += [[x, 1500, 0, x, 1500, 3000] for x in (30000, 32480, 34960)]
    beams = _ok(
        await tools["create_structural_members"](
            {**_SHS100, "segments": floor, "name": "Floor", "define": "none"}
        )
    )["members"]
    assert beams["groups"] == 7
    before = beams["bodies_after"]
    trim = _ok(
        await tools["create_trim_extend"](
            {"members": ["Floor"], "boundary": ["Columns"]}
        )
    )["trim"]
    # SolidWorks returns some beams trimmed between two columns twice; the
    # twins are deleted, so the part keeps one body per member.
    assert not trim.get("split_pieces")
    assert trim["bodies_after"] == before
    joints = _ok(await tools["check_joints"]({"members": ["Floor"]}))["joints"]
    assert joints["overlaps"]["count"] == 0
