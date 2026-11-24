"""The plate and feature-tree methods pass through the circuit breaker unchanged.

The server wraps its adapter: a method the wrapper doesn't forward falls to
the base adapter's "not implemented" (seen in an agent's run: create_plates
failed through the real server while direct adapter tests passed).
"""

from __future__ import annotations

from typing import Any

import pytest

from solidworks_mcp.adapters.circuit_breaker import CircuitBreakerAdapter

from .weldment_plates_calls import CALLS, IDS, recorder


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "args", "kwargs"), CALLS, ids=IDS)
async def test_each_plate_and_tree_method_passes_through(
    name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    result = await getattr(CircuitBreakerAdapter(adapter=recorder()), name)(
        *args, **kwargs
    )

    assert result.data[name][0] == args
