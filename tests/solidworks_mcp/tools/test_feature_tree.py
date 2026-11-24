"""Tests for the feature-tree tools, through the mock adapter."""

from __future__ import annotations

from typing import Any

import pytest

from solidworks_mcp.tools.feature_tree import (
    SetFeatureFoldersInput,
    register_feature_tree_tools,
)
from solidworks_mcp.tools.weldments import register_weldment_tools

_MEMBERS = {
    "standard": "DIN",
    "profile_type": "hea",
    "size": "HE300A",
}


async def _tools(mcp_server: Any, adapter: Any) -> dict[str, Any]:
    await register_weldment_tools(mcp_server, adapter, {})
    await register_feature_tree_tools(mcp_server, adapter, {})
    return {t.name: t.fn for t in await mcp_server.list_tools()}


def test_folders_need_something_to_do() -> None:
    with pytest.raises(ValueError, match="give rename or folders"):
        SetFeatureFoldersInput()
    with pytest.raises(ValueError):
        SetFeatureFoldersInput(folders=[{"name": "Empty", "features": []}])


@pytest.mark.asyncio
async def test_a_tree_built_in_folders_checks_clean(mcp_server, mock_adapter) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_part()
    made = await tools["create_structural_members"](
        {
            **_MEMBERS,
            "segments": [[0, 0, 0, 0, 3000, 0]],
            "name": "Columns",
            "folder": "Frame",
        }
    )
    assert made["members"]["sketch"] == "Columns skeleton"
    assert made["members"]["skeleton"]["definition"] == "fully defined"
    assert made["members"]["folder"]["moved"] == ["Columns skeleton", "Columns"]
    checked = await tools["check_feature_tree"]({})
    assert checked["status"] == "success"
    assert checked["audit"]["verdict"] == "clean"
    assert checked["audit"]["folders"] == [{"name": "Frame", "features": 2}]


@pytest.mark.asyncio
async def test_a_loose_tree_is_reported_then_tidied(mcp_server, mock_adapter) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    await mock_adapter.create_part()
    await tools["create_structural_members"](
        {
            **_MEMBERS,
            "segments": [[0, 0, 0, 0, 3000, 0]],
            "sketch_name": "3DSketch1",
            "define": "none",
        }
    )
    for index in range(14):
        await tools["create_structural_members"](
            {
                **_MEMBERS,
                "segments": [[index * 1000, 0, 0, index * 1000, 3000, 0]],
                "name": f"Post {index}",
            }
        )
    report = (await tools["check_feature_tree"]({"max_listed": 3}))["audit"]
    assert report["verdict"] == "needs work"
    assert report["not_fully_defined"] == ["3DSketch1"]
    assert report["generic_names"] == ["3DSketch1"]
    assert len(report["issues"]) == 3

    tidied = await tools["set_feature_folders"](
        {
            "rename": {"3DSketch1": "Corner post skeleton", "Nope": "X"},
            "folders": [
                {"name": "Posts", "features": ["Corner post skeleton", "Ghost"]}
            ],
        }
    )
    assert tidied["tree"]["renamed"] == {"3DSketch1": "Corner post skeleton"}
    assert tidied["tree"]["unknown_names"] == ["Nope"]
    assert tidied["tree"]["folders"][0]["missing"] == ["Ghost"]
    assert "warning" in tidied


@pytest.mark.asyncio
async def test_the_tree_tools_need_a_model(mcp_server, mock_adapter) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    assert (await tools["check_feature_tree"]({}))["status"] == "error"
    assert (await tools["set_feature_folders"]({"rename": {"A": "B"}}))[
        "status"
    ] == "error"
