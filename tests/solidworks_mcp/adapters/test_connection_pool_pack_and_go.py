"""Pack and Go passes through the connection pool to a pooled adapter."""

from __future__ import annotations

from typing import Any

import pytest

from solidworks_mcp.adapters.connection_pool import ConnectionPoolAdapter


def packer() -> Any:
    """A mock adapter whose Pack and Go answers with the arguments it got."""
    from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
    from solidworks_mcp.adapters.mock_adapter import MockSolidWorksAdapter

    async def pack(
        self: Any, source_path: str, target_dir: str
    ) -> AdapterResult[dict[str, str]]:
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={"source_path": source_path, "target_dir": target_dir},
        )

    return type("_Packer", (MockSolidWorksAdapter,), {"pack_and_go_assembly": pack})({})


@pytest.mark.asyncio
async def test_pack_and_go_passes_through() -> None:
    pool = ConnectionPoolAdapter(adapter_factory=packer, max_size=1)
    await pool.connect()
    try:
        result = await pool.pack_and_go_assembly(
            source_path="C:/a/frame.sldasm", target_dir="C:/b"
        )
    finally:
        await pool.disconnect()

    assert result.data == {"source_path": "C:/a/frame.sldasm", "target_dir": "C:/b"}
