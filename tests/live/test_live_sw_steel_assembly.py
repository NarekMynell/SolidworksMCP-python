"""Fast component placement (steel-structures toolkit), checked on real SolidWorks.

A 1000 x 100 x 50 mm bar whose origin sits at one corner (so its origin and
its bounding box's middle differ), then an assembly built the way a steel
frame is: many members placed by coordinates in one call.

1. ``add_components``: three bars in one call, the second turned 90 degrees
   about z, the third centred on its point; all fixed. Each placement is read
   back from SolidWorks and the parts open without windows.
2. ``insert_component`` with a turn and the default centre anchor.
3. ``set_component_position``: one bar placed absolutely, two shifted.
4. ``set_component_state`` with ``components``: three floated, then fixed.
5. ``delete_feature`` on a component: the tree is listed without walking
   every component's features.

Collected only with ``SOLIDWORKS_MCP_RUN_REAL_INTEGRATION=1`` on Windows.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

BAR = (1000.0, 100.0, 50.0)


async def _tools(adapter: Any) -> dict[str, Any]:
    """Every tool, registered against ``adapter``: name -> function."""
    from fastmcp import FastMCP

    from solidworks_mcp.tools import register_tools

    mcp = FastMCP("steel-assembly-live")
    await register_tools(mcp, adapter, {})
    return {t.name: t.fn for t in await mcp.list_tools()}


def _ok(reply: dict[str, Any]) -> dict[str, Any]:
    assert reply["status"] == "success", reply
    return reply


def _box_middle(adapter: Any, name: str) -> list[float]:
    """The middle of a component's bounding box, read from SolidWorks (mm)."""
    from solidworks_mcp.adapters import sw_type_info
    from solidworks_mcp.adapters.solidworks.io import _find_components

    assembly = sw_type_info.flagged(adapter.currentModel, "IAssemblyDoc")
    component = _find_components(adapter, assembly, [name])[name]
    box = list(component.GetBox(False, False))
    return [round((box[i] + box[i + 3]) * 500.0, 3) for i in range(3)]


def _visible_titles(adapter: Any) -> list[str]:
    """Titles of the documents SolidWorks shows in a window."""
    from solidworks_mcp.adapters import sw_type_info

    titles = []
    doc = adapter.swApp.GetFirstDocument()
    while doc is not None:
        doc = sw_type_info.flagged(doc, "IModelDoc2")
        if doc.Visible:
            titles.append(str(doc.GetTitle()))
        doc = doc.GetNext()
    return titles


async def test_a_frame_of_bars_placed_by_coordinates(
    scratch: Any, tmp_path: Path
) -> None:
    tools = await _tools(scratch.adapter)
    adapter = scratch.adapter

    # The bar: a rectangle from the origin on the Top plane, extruded up.
    _ok(await tools["create_part"]({"name": "bar"}))
    sketch = _ok(await tools["create_sketch"]({"plane": "Top"}))
    _ok(await tools["add_rectangle"]({"x1": 0, "y1": 0, "x2": BAR[0], "y2": BAR[1]}))
    _ok(await tools["exit_sketch"]())
    _ok(
        await tools["create_extrusion"](
            {"sketch_name": sketch["sketch"]["name"], "depth": BAR[2]}
        )
    )
    bar = tmp_path / "bar.SLDPRT"
    saved = await adapter.save_file(str(bar))
    assert saved.is_success, saved.error
    scratch.written_files.append(str(bar))
    closed = await tools["close_model"]({"save": False})
    assert closed["status"] == "success", closed

    _ok(await tools["create_assembly"]({"name": "frame"}))

    # 1. Three bars in one call.
    started = time.perf_counter()
    added = _ok(
        await tools["add_components"](
            {
                "items": [
                    {"file_path": str(bar)},
                    {"file_path": str(bar), "x": 2000, "rz": 90},
                    {
                        "file_path": str(bar),
                        "x": 0,
                        "y": 0,
                        "z": 500,
                        "anchor": "center",
                    },
                ],
                "fix": True,
            }
        )
    )
    took = time.perf_counter() - started
    print(f"[live] add_components: 3 bars in {took:.2f} s")
    placed = added["components"]["added"]
    assert [p["component"] for p in placed] == ["bar-1", "bar-2", "bar-3"]
    assert placed[0]["origin_mm"] == pytest.approx([0, 0, 0], abs=1e-6)
    assert placed[1]["origin_mm"] == pytest.approx([2000, 0, 0], abs=1e-6)
    assert placed[1]["rotation_deg"] == pytest.approx([0, 0, 90], abs=1e-6)
    assert _box_middle(adapter, "bar-3") == pytest.approx([0, 0, 500], abs=1e-3)
    assert added["components"]["components_after"] == 3
    assert "bar" not in [t.lower() for t in _visible_titles(adapter)]
    assert took < 30

    # 2. One more, turned, centred on its point (the default anchor).
    one = _ok(
        await tools["insert_component"](
            {"file_path": str(bar), "x": 4000, "y": 0, "z": 0, "rz": 90}
        )
    )
    name = one["component"]["component"]
    assert _box_middle(adapter, name) == pytest.approx([4000, 0, 0], abs=1e-3)
    assert one["component"]["rotation_deg"] == pytest.approx([0, 0, 90], abs=1e-6)

    # 3. Moves: absolute (z only), then two shifted together.
    up = _ok(
        await tools["set_component_position"]({"components": ["bar-1"], "z": 1500})
    )
    assert up["position"]["moved"][0]["after"]["origin_mm"] == pytest.approx(
        [0, 0, 1500], abs=1e-6
    )
    shifted = _ok(
        await tools["set_component_position"](
            {"components": ["bar-2", "bar-3"], "y": -20, "relative": True}
        )
    )
    befores = [m["before"]["origin_mm"][1] for m in shifted["position"]["moved"]]
    afters = [m["after"]["origin_mm"][1] for m in shifted["position"]["moved"]]
    assert [a - b for a, b in zip(afters, befores, strict=True)] == pytest.approx(
        [-20, -20], abs=1e-6
    )

    # 4. Three floated, then fixed again, one call each.
    names = ["bar-1", "bar-2", "bar-3"]
    floated = _ok(
        await tools["set_component_state"]({"components": names, "fixed": False})
    )
    assert [e["after"]["fixed"] for e in floated["state"]["components"]] == [False] * 3
    fixed = _ok(
        await tools["set_component_state"]({"components": names, "fixed": True})
    )
    assert [e["after"]["fixed"] for e in fixed["state"]["components"]] == [True] * 3

    # 5. Delete one component; the listing stays on the assembly's own tree.
    started = time.perf_counter()
    deleted = _ok(await tools["delete_feature"]({"name": name}))
    took = time.perf_counter() - started
    print(f"[live] delete_feature {name}: {took:.2f} s")
    assert name in deleted["data"]["removed_features"]
    assert took < 15
    listed = _ok(await tools["list_components"]())
    assert sorted(listed["components"]) == names
