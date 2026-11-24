"""One call of each plate and feature-tree adapter method, shared by the layer tests."""

from __future__ import annotations

from typing import Any

CALLS: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
    ("create_plates", ({"plates": [{"name": "Base plate"}], "folder": "Plates"},), {}),
    ("export_plate_dxfs", ({"output_dir": "C:/dxf"},), {}),
    ("create_vent_holes", ({"members": ["Rails"], "diameter": 12},), {}),
    ("set_feature_folders", ({"rename": {"Plane7": "Splice plane"}},), {}),
    ("check_feature_tree", ({"max_listed": 5},), {}),
]

IDS = [call[0] for call in CALLS]


def recorder() -> Any:
    """A mock adapter whose plate and tree methods record their arguments."""
    from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
    from solidworks_mcp.adapters.mock_adapter import MockSolidWorksAdapter

    def method(name: str) -> Any:
        async def call(self: Any, *args: Any, **kwargs: Any) -> AdapterResult[Any]:
            return AdapterResult(
                status=AdapterResultStatus.SUCCESS, data={name: [args, kwargs]}
            )

        return call

    body = {name: method(name) for name in IDS}
    return type("_Recorder", (MockSolidWorksAdapter,), body)({})
