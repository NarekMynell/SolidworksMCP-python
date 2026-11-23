"""Tests for the assembly tools: what reaches the adapter and what comes back."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
from solidworks_mcp.tools.assembly import (
    PatternComponentsInput,
    register_assembly_tools,
)


def _ok(data: Any) -> AdapterResult[Any]:
    return AdapterResult(status=AdapterResultStatus.SUCCESS, data=data)


async def _tools(mcp_server: Any, adapter: Any) -> dict[str, Any]:
    await register_assembly_tools(mcp_server, adapter, {})
    return {t.name: t.fn for t in await mcp_server.list_tools()}


@pytest.mark.asyncio
async def test_explode_by_steps_or_automatically_and_collapse(mcp_server) -> None:
    adapter = Mock()
    adapter.create_exploded_view = AsyncMock(return_value=_ok({"view": "ExplView1"}))
    adapter.set_exploded_view = AsyncMock(return_value=_ok({"exploded": False}))
    tools = await _tools(mcp_server, adapter)

    stepped = await tools["create_exploded_view"](
        {"steps": [{"components": ["pin-1"], "direction": "y", "distance": 40}]}
    )
    auto = await tools["create_exploded_view"]({})
    collapsed = await tools["set_exploded_view"]({"exploded": False})
    shown = await tools["set_exploded_view"]({"exploded": True})
    bad = await tools["create_exploded_view"](
        {"steps": [{"components": ["pin-1"], "direction": "w", "distance": 1}]}
    )

    first = adapter.create_exploded_view.await_args_list[0].args[0]
    assert first == [
        {"components": ["pin-1"], "direction": "y", "distance": 40.0, "reverse": False}
    ]
    assert adapter.create_exploded_view.await_args_list[1].args == (None,)
    assert stepped["message"] == "Exploded the assembly in 1 step(s)"
    assert auto["message"] == "Exploded the assembly in automatic step(s)"
    assert collapsed["message"] == "The assembly shows collapsed"
    assert shown["message"] == "The assembly shows exploded"
    assert bad["error_type"] == "invalid_input"


@pytest.mark.asyncio
async def test_pattern_mirror_and_replace(mcp_server) -> None:
    adapter = Mock()
    adapter.pattern_components = AsyncMock(
        return_value=_ok({"added": ["pin-2", "pin-3", "pin-4"]})
    )
    adapter.mirror_components = AsyncMock(return_value=_ok({"added": ["foot-2"]}))
    adapter.replace_component = AsyncMock(
        return_value=_ok({"replaced": {"pin-1": "a", "pin-2": "a"}})
    )
    tools = await _tools(mcp_server, adapter)

    pattern = await tools["pattern_components"](
        {"components": ["pin-1"], "axis": "Axis1", "count": 4, "spacing": 25}
    )
    mirror = await tools["mirror_components"](
        {"components": ["foot-1"], "plane": "Right Plane"}
    )
    replace = await tools["replace_component"](
        {"component": "pin-1", "file_path": "C:/p/pin45.sldprt", "all_instances": True}
    )

    adapter.pattern_components.assert_awaited_once_with(
        ["pin-1"], "Axis1", 4, "linear", 25.0, None
    )
    assert pattern["message"] == "Added 3 component(s) in a linear pattern"
    assert mirror["message"] == "Mirrored 1 component(s) about Right Plane"
    assert replace["message"] == "Replaced 2 instance(s) with C:/p/pin45.sldprt"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"components": ["pin-1"], "axis": "A", "count": 3}, "needs spacing"),
        ({"components": ["pin-1"], "axis": "A", "count": 1, "spacing": 5}, "count"),
    ],
)
def test_a_pattern_needs_its_spacing_and_two_instances(
    kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        PatternComponentsInput(**kwargs)


def test_a_circular_pattern_needs_no_spacing() -> None:
    made = PatternComponentsInput(
        components=["pin-1"], axis="A", count=6, kind="circular", angle=360
    )

    assert made.spacing is None


@pytest.mark.asyncio
async def test_add_components_sends_every_item_in_one_call(mcp_server) -> None:
    adapter = Mock()
    adapter.add_components = AsyncMock(
        return_value=_ok(
            {
                "added": [{"index": 0, "component": "col-1"}],
                "failed": [
                    {"index": 1, "file_path": "x.sldprt", "error": "file not found"}
                ],
                "fixed": True,
            }
        )
    )
    tools = await _tools(mcp_server, adapter)

    reply = await tools["add_components"](
        {
            "items": [
                {"file_path": "C:/p/col.sldprt", "x": 9000, "rz": 90},
                {"file_path": "x.sldprt"},
            ],
            "fix": True,
        }
    )
    empty = await tools["add_components"]({"items": []})

    items, fix = adapter.add_components.await_args.args
    assert fix is True
    assert items[0] == {
        "file_path": "C:/p/col.sldprt",
        "x": 9000.0,
        "y": 0.0,
        "z": 0.0,
        "rx": 0.0,
        "ry": 0.0,
        "rz": 90.0,
        "anchor": "origin",
    }
    assert reply["message"] == "Added 1 of 2 component(s) and fixed them"
    assert reply["warning"].startswith("1 item(s) failed: #1 x.sldprt: file not found")
    assert empty["error_type"] == "invalid_input"


@pytest.mark.asyncio
async def test_set_component_position_places_or_shifts(mcp_server) -> None:
    adapter = Mock()
    adapter.set_component_position = AsyncMock(
        return_value=_ok({"moved": [{"component": "a-1"}, {"component": "a-2"}]})
    )
    tools = await _tools(mcp_server, adapter)

    shifted = await tools["set_component_position"](
        {"components": ["a-1", "a-2"], "z": 20, "relative": True}
    )
    placed = await tools["set_component_position"]({"components": ["a-1"], "rz": 90})
    nothing = await tools["set_component_position"]({"components": ["a-1"]})
    pile = await tools["set_component_position"]({"components": ["a-1", "a-2"], "x": 0})

    assert adapter.set_component_position.await_args_list[0].args == (
        ["a-1", "a-2"],
        None,
        None,
        20.0,
        None,
        None,
        None,
        True,
    )
    assert shifted["message"] == "Shifted 2 component(s)"
    assert placed["message"] == "Placed 2 component(s)"
    assert nothing["error_type"] == "invalid_input"
    assert "same spot" in pile["message"]
