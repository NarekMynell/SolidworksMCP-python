"""Weldments (steel-structures toolkit), checked on real SolidWorks.

A 12 m Warren truss, 1.5 m deep, six panels, as a shop would get it:

1. ``list_weldment_profiles`` finds the sizes by the names agents use:
   ``HEB400`` is DIN's ``HE400B``, ``100x100x5`` BSI's ``RHS100x100x5``.
2. ``create_structural_members``: the chords as two continuous SHS
   100x100x5 members, then the zig-zag web, SHS 60x60x4, in one chain.
3. ``set_material``, then ``get_cut_list`` with a CSV: the items add up to
   the part's own mass, the web members are mitred where they meet, and no
   profile library part is left open.

Collected only with ``SOLIDWORKS_MCP_RUN_REAL_INTEGRATION=1`` on Windows.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

SPAN, DEPTH, PANELS = 12000.0, 1500.0, 6


async def _tools(adapter: Any) -> dict[str, Any]:
    """Every tool, registered against ``adapter``: name -> function."""
    from fastmcp import FastMCP

    from solidworks_mcp.tools import register_tools

    mcp = FastMCP("weldments-live")
    await register_tools(mcp, adapter, {})
    return {t.name: t.fn for t in await mcp.list_tools()}


def _ok(reply: dict[str, Any]) -> dict[str, Any]:
    assert reply["status"] == "success", reply
    return reply


async def test_a_warren_truss_and_its_cut_list(scratch: Any, tmp_path: Path) -> None:
    tools = await _tools(scratch.adapter)

    # 1. The library, searched by the names agents use.
    heb = _ok(await tools["list_weldment_profiles"]({"query": "HEB400"}))
    assert [m["size"] for m in heb["profiles"]["matches"]] == ["HE400B"]
    shs = _ok(
        await tools["list_weldment_profiles"]({"query": "100x100x5", "standard": "BSI"})
    )
    assert "RHS100x100x5" in [m["size"] for m in shs["profiles"]["matches"]]

    # 2. The truss: continuous chords, then the web in one chain.
    _ok(await tools["create_part"]({"name": "truss"}))
    half = SPAN / PANELS / 2
    chords = _ok(
        await tools["create_structural_members"](
            {
                "standard": "BSI",
                "profile_type": "shs",
                "size": "RHS100x100x5",
                "segments": [
                    [0, 0, 0, SPAN, 0, 0],
                    [half, DEPTH, 0, SPAN - half, DEPTH, 0],
                ],
                "name": "Chords",
                "sketch_name": "Chord axes",
            }
        )
    )
    assert chords["members"]["feature"] == "Chords"
    web = []
    for i in range(PANELS):
        x = 2 * half * i
        web.append([x, 0, 0, x + half, DEPTH, 0])
        web.append([x + half, DEPTH, 0, x + 2 * half, 0, 0])
    members = _ok(
        await tools["create_structural_members"](
            {
                "standard": "BSI",
                "profile_type": "shs",
                "size": "RHS60x60x4",
                "segments": web,
                "name": "Web",
            }
        )
    )
    assert (members["members"]["groups"], members["members"]["grouping"]) == (1, "auto")
    assert members["members"]["bodies_after"] == 2 + 2 * PANELS

    # 3. Steel, then the cut list, which adds up to the part's own mass.
    _ok(await tools["set_material"]({"material_name": "Plain Carbon Steel"}))
    out = tmp_path / "truss-cut-list.csv"
    cut = _ok(await tools["get_cut_list"]({"export_path": str(out)}))
    scratch.written_files.append(str(out))
    listed = cut["cut_list"]
    lengths = sorted(i["length_mm"] for i in listed["items"])
    assert lengths[-1] == pytest.approx(SPAN, abs=0.01)
    assert lengths[-2] == pytest.approx(SPAN - 2 * half, abs=0.01)
    assert listed["pieces"] == 2 + 2 * PANELS
    assert any(i["angle1_deg"] and i["angle2_deg"] for i in listed["items"])
    mass = _ok(await tools["get_mass_properties"]({}))
    part_kg = float(mass["mass_properties"]["mass"]["value"])
    assert listed["total_mass_kg"] == pytest.approx(part_kg, rel=1e-6)
    with open(out, encoding="utf-8-sig") as handle:
        assert len(list(csv.DictReader(handle))) == listed["item_count"]
    opened = _ok(await tools["list_open_documents"]())
    paths = [str(d.get("path", "")).lower() for d in opened.get("documents", [])]
    assert not [p for p in paths if p.endswith(".sldlfp")]
    print(f"[live] truss: {listed['pieces']} pieces, {listed['total_mass_kg']} kg")
