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

    # 2. The truss: continuous chords, then the web, each diagonal its own piece
    # (a chain stops at a node on a member already there), trimmed to the chords.
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
    assert (members["members"]["groups"], members["members"]["grouping"]) == (
        2 * PANELS,
        "auto",
    )
    assert members["members"]["bodies_after"] == 2 + 2 * PANELS
    _ok(
        await tools["create_trim_extend"](
            {"members": ["Web"], "boundary": ["Chords"], "corner_type": "end_trim"}
        )
    )

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


async def test_a_platform_trimmed_where_its_members_meet(scratch: Any) -> None:
    """A 3 x 2 m platform drawn on centre lines, then trimmed as it is built.

    HE160B columns, IPE240 beams sitting on them, IPE160 joists between the
    beams with their tops flush: the joists are coped to the beams with a
    5 mm weld gap, so each is 2000 - 6.2 (beam web) - 2 x 5 = 1983.8 mm. Two
    SHS rails meeting at a corner are mitred to 45 degrees. A name that is no
    member is refused with the members the part has.
    """
    tools = await _tools(scratch.adapter)
    _ok(await tools["create_part"]({"name": "platform"}))

    async def members(size: str, kind: str, name: str, segments: list) -> None:
        _ok(
            await tools["create_structural_members"](
                {
                    "standard": "DIN" if kind != "shs" else "BSI",
                    "profile_type": kind,
                    "size": size,
                    "segments": segments,
                    "name": name,
                }
            )
        )

    corners = [(0, 0), (3000, 0), (0, 2000), (3000, 2000)]
    await members(
        "HE160B", "hea", "Columns", [[x, 0, z, x, 2260, z] for x, z in corners]
    )
    await members(
        "IPE240",
        "ipe",
        "Beams",
        [[-80, 2380, z, 3080, 2380, z] for z in (0, 2000)],
    )
    await members(
        "IPE160", "ipe", "Joists", [[x, 2420, 0, x, 2420, 2000] for x in (1000, 2000)]
    )
    await members("RHS60x60x4", "shs", "Rail A", [[0, 3000, 0, 1000, 3000, 0]])
    await members("RHS60x60x4", "shs", "Rail B", [[1000, 3000, 0, 1000, 3000, 1000]])

    coped = _ok(
        await tools["create_trim_extend"](
            {
                "members": ["Joists"],
                "boundary": ["Beams"],
                "cut": "coped",
                "weld_gap_mm": 5,
                "name": "Joists to beams",
            }
        )
    )
    mitred = _ok(
        await tools["create_trim_extend"](
            {"members": ["Rail A"], "boundary": ["Rail B"], "corner_type": "miter"}
        )
    )
    wrong = await tools["create_trim_extend"](
        {"members": ["Purlins"], "boundary": ["Beams"]}
    )
    cut = _ok(await tools["get_cut_list"]({}))["cut_list"]["items"]

    assert coped["trim"]["feature"] == "Joists to beams"
    assert len(coped["trim"]["members"]) == 2 and coped["trim"]["unchanged"] == []
    assert all(m["volume_change_mm3"] < 0 for m in coped["trim"]["members"])
    joists = [i for i in cut if i["description"] == "IPE160"]
    assert [(i["quantity"], i["length_mm"]) for i in joists] == [(2, 1983.8)]
    assert mitred["trim"]["unchanged"] == []
    rails = [i for i in cut if i["description"] == "RHS60x60x4"]
    # Both rails come out alike, one square end and one mitred: one item of two.
    assert [
        (i["quantity"], sorted((i["angle1_deg"], i["angle2_deg"]))) for i in rails
    ] == [(2, [0.0, 45.0])]
    assert wrong["status"] == "error"
    assert "No structural member or body named 'Purlins'" in wrong["message"]
    assert "Columns, Beams, Joists, Rail A, Rail B" in wrong["message"]
    print(
        f"[live] platform cut list: {[(i['description'], i['quantity'], i['length_mm']) for i in cut]}"
    )


async def test_a_lattice_column_checked_trimmed_and_capped(
    scratch: Any, tmp_path: Path
) -> None:
    """A 2 m lattice column as a lattice generator writes it, joints measured.

    Four SHS 100x100x5 chords and SHS 60x60x4 diagonals on all four faces
    come from segments files. Drawn node to node, the diagonals run into the
    chords (``check_joints`` counts the pairs). Trimmed to the chords'
    faces, nothing overlaps: the diagonals meet the chords at 36.87
    degrees, which leaves a 33 mm gap between the two at each node, and the
    eight chord ends are the open ends. ``create_end_caps`` closes them, and
    the check comes out clean.
    """
    import json

    tools = await _tools(scratch.adapter)
    _ok(await tools["create_part"]({"name": "lattice"}))
    chords = tmp_path / "column.chords.json"
    chords.write_text(
        json.dumps([[x, 0, z, x, 2000, z] for x in (-300, 300) for z in (-300, 300)]),
        encoding="utf-8",
    )
    braces = tmp_path / "column.braces.json"
    braces.write_text(
        json.dumps(
            {
                "segments": [
                    [-300, 200, -300, 300, 1000, -300],
                    [300, 1000, -300, -300, 1800, -300],
                    [300, 200, -300, 300, 1000, 300],
                    [300, 1000, 300, 300, 1800, -300],
                    [300, 200, 300, -300, 1000, 300],
                    [-300, 1000, 300, 300, 1800, 300],
                    [-300, 200, 300, -300, 1000, -300],
                    [-300, 1000, -300, -300, 1800, 300],
                ]
            }
        ),
        encoding="utf-8",
    )
    for name, size, path in (
        ("Chords", "RHS100x100x5", chords),
        ("Braces", "RHS60x60x4", braces),
    ):
        _ok(
            await tools["create_structural_members"](
                {
                    "standard": "BSI",
                    "profile_type": "shs",
                    "size": size,
                    "segments_file": str(path),
                    "grouping": "each",
                    "corner_treatment": "none",
                    "name": name,
                }
            )
        )

    raw = _ok(await tools["check_joints"]())["joints"]
    _ok(
        await tools["create_trim_extend"](
            {"members": ["Braces"], "boundary": ["Chords"]}
        )
    )
    trimmed = _ok(await tools["check_joints"]())["joints"]
    capped = _ok(await tools["create_end_caps"]({"open_ends": True, "thickness_mm": 5}))
    done = _ok(await tools["check_joints"]())["joints"]

    assert raw["overlaps"]["count"] >= 8 and raw["clean"] is False
    assert raw["members"] == 12 and raw["loose"] == []
    assert trimmed["overlaps"]["count"] == 0, trimmed["overlaps"]["pairs"]
    assert trimmed["partly_open_ends"]["count"] == 0, trimmed["partly_open_ends"]
    assert trimmed["open_ends"]["count"] == 8, trimmed["open_ends"]["ends"]
    assert {round(e["at_mm"][1]) for e in trimmed["open_ends"]["ends"]} == {0, 2000}
    assert trimmed["clean"] is True and trimmed["groups"] == 1
    assert capped["caps"]["caps"] == 8 and capped["caps"]["bodies_after"] == 20
    assert done["open_ends"]["count"] == 0 and done["overlaps"]["count"] == 0
    assert done["clean"] is True and done["bodies"] == 20
    print(
        f"[live] lattice column: {raw['overlaps']['count']} overlaps drawn, "
        f"{trimmed['open_ends']['count']} open ends trimmed, "
        f"{done['seconds']} s to check {done['bodies']} bodies"
    )


async def test_a_weldment_drawing_carries_its_cut_list(
    scratch: Any, tmp_path: Path
) -> None:
    """A weldment's A3 drawing: its cut list above the isometric, no profile
    dimensions, then a balloon per cut-list item on the isometric's left.

    Two live findings shaped it (SW 2025 SP4): ``InsertWeldmentTable`` returns
    nothing for the view's "Default<As Machined>" configuration, and the
    template's table is 199 mm wide until its columns are fitted.
    """
    tools = await _tools(scratch.adapter)
    _ok(await tools["create_part"]({"name": "frame"}))
    for name, size, segments in (
        ("Posts", "RHS100x100x5", [[x, 0, 0, x, 2000, 0] for x in (0, 3000)]),
        ("Rail", "RHS60x60x4", [[0, 2030, 0, 3000, 2030, 0]]),
    ):
        _ok(
            await tools["create_structural_members"](
                {
                    "standard": "BSI",
                    "profile_type": "shs",
                    "size": size,
                    "segments": segments,
                    "name": name,
                }
            )
        )
    _ok(await tools["set_material"]({"material_name": "1.0037 (S235JR)"}))
    part = tmp_path / "frame.sldprt"
    _ok(await tools["save_file"]({"file_path": str(part)}))
    scratch.written_files.append(str(part))

    drawing = _ok(
        await tools["generate_drawing_package"](
            {"model_path": str(part), "drawing_number": "FR-001", "author": "MCP"}
        )
    )
    data = drawing["data"]
    scratch.written_files += [data["drawing_path"], data["pdf_path"]]
    balloons = _ok(
        await tools["add_balloons"]({"view_name": "Drawing View4", "layout": "left"})
    )

    assert data["cut_list"]["row_count"] == 3  # the header and two items
    box = data["cut_list"]["table_box_mm"]
    assert box[2] - box[0] < 150  # fitted: the template's table is 199 mm
    # no profile sketch sizes, only the frame's overall width and height
    assert data["dimensions"]["dimensions_added"] >= 2
    assert {"width", "height"} <= set(data["dimensions"]["overall"]["sizes"])
    assert balloons["balloons"]["items"] == ["1", "2"]
    print(f"[live] weldment drawing: table {box} mm, scale {data['scale']}")
