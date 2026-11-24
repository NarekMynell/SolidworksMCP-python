"""Pack and Go passes through the circuit breaker to the adapter it wraps.

The server's own adapter stack once answered every pack_and_go_assembly call
with "'CircuitBreakerAdapter' object has no attribute 'pack_and_go_assembly'".
"""

from __future__ import annotations

import pytest

from solidworks_mcp.adapters.circuit_breaker import CircuitBreakerAdapter

from .test_connection_pool_pack_and_go import packer


@pytest.mark.asyncio
async def test_pack_and_go_passes_through() -> None:
    result = await CircuitBreakerAdapter(adapter=packer()).pack_and_go_assembly(
        source_path="C:/a/frame.sldasm", target_dir="C:/b"
    )

    assert result.data == {"source_path": "C:/a/frame.sldasm", "target_dir": "C:/b"}
