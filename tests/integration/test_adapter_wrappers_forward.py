"""Every adapter method a tool calls reaches the real adapter through the server's wrappers.

The server runs its tools on ``ConnectionPoolAdapter`` and
``CircuitBreakerAdapter`` around the COM adapter. A method a wrapper doesn't
define falls to the base adapter's "not implemented" stub, or raises
AttributeError when the base has none: both reached agents' runs
(``create_plates``, then ``pack_and_go_assembly``) while every direct adapter
test passed. This reads the adapter calls out of the tool modules and checks
that each wrapper defines every one of them itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from solidworks_mcp.adapters.base import SolidWorksAdapter
from solidworks_mcp.adapters.circuit_breaker import CircuitBreakerAdapter
from solidworks_mcp.adapters.connection_pool import ConnectionPoolAdapter
from solidworks_mcp.adapters.pywin32_adapter import PyWin32Adapter

TOOLS = Path(__file__).resolve().parents[2] / "src" / "solidworks_mcp" / "tools"
#: Base-class methods that call another adapter method, which is forwarded.
ALIASES = {"add_sketch_circle": "add_circle"}
_CALL = re.compile(r"adapter\.([a-z_][a-z0-9_]*)\(")
_NAMED = re.compile(r'(?:require_capability\(\s*adapter,\s*|_supports\()"([a-z0-9_]+)"')


def tool_calls() -> list[str]:
    """The adapter methods the tool modules call that the COM adapter has."""
    names: set[str] = set()
    for module in TOOLS.rglob("*.py"):
        text = module.read_text(encoding="utf-8")
        names.update(_CALL.findall(text))
        names.update(_NAMED.findall(text))
    return sorted(n for n in names if callable(getattr(PyWin32Adapter, n, None)))


def test_the_tool_modules_call_the_adapter() -> None:
    assert len(tool_calls()) > 100


@pytest.mark.parametrize(
    "wrapper", [CircuitBreakerAdapter, ConnectionPoolAdapter], ids=lambda c: c.__name__
)
def test_each_wrapper_forwards_every_method_a_tool_calls(wrapper: type) -> None:
    left_to_base = [
        name
        for name in tool_calls()
        if getattr(wrapper, name, None) is None
        or (
            name not in ALIASES
            and getattr(wrapper, name) is getattr(SolidWorksAdapter, name, None)
        )
    ]

    assert left_to_base == []
