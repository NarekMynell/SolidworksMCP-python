"""The user's SolidWorks options: off during each call, back after, on real SolidWorks.

The five options automation needs off (``user_settings.AUTOMATION_OFF``) are
switched on first, as a designer who sketches by hand has them; the user's
own values come back at the end.

1. A part, a sketch, two lines (30 and 80 mm) and a 50 mm dimension on the
   first, each through the adapter. Inside every call all five are off;
   after it, all five are on again. The dimension call returned (no Modify
   box held it) with the first line at 50 mm and the second still 80 mm.
2. A note a killed server left (an ended process's id) is put back by the
   next connect's ``recover``, and removed.
3. What the switching costs: a call that does nothing, 20 times with the
   options on (each switched off and back) and 20 times with them off.

Writes ``docs/proofs/user-settings.json`` before it asserts.
Collected only with ``SOLIDWORKS_MCP_RUN_REAL_INTEGRATION=1`` on Windows.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

RECEIPT = Path(__file__).parents[2] / "docs" / "proofs" / "user-settings.json"
CALLS = 20


def _read(adapter: Any) -> dict[int, bool]:
    from solidworks_mcp.adapters.solidworks.user_settings import AUTOMATION_OFF

    return {t: bool(adapter.swApp.GetUserPreferenceToggle(t)) for t in AUTOMATION_OFF}


def _set(adapter: Any, values: dict[int, bool]) -> None:
    for toggle, value in values.items():
        adapter.swApp.SetUserPreferenceToggle(toggle, value)


def _length_mm(adapter: Any, entity_id: str) -> float:
    from solidworks_mcp.adapters import sw_type_info

    segment = sw_type_info.flagged(
        adapter._sketch_entities[entity_id], "ISketchSegment"
    )
    return round(float(segment.GetLength()) * 1000.0, 4)


async def test_options_are_off_in_each_call_and_back_after_it(
    scratch: Any, tmp_path: Path
) -> None:
    from solidworks_mcp.adapters.solidworks.user_settings import UserSettings

    adapter = scratch.adapter
    users = _read(adapter)
    on = dict.fromkeys(users, True)
    inside: list[tuple[str, dict[int, bool]]] = []
    after: list[tuple[str, dict[int, bool]]] = []
    real = adapter._handle_com_operation

    def watched(name: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        def probe(*a: Any, **k: Any) -> Any:
            inside.append((name, _read(adapter)))
            return func(*a, **k)

        return real(name, probe, *args, **kwargs)

    receipt: dict[str, Any] = {"checked_at": datetime.now(UTC).isoformat()}
    try:
        _set(adapter, on)
        adapter._handle_com_operation = watched
        steps = {}
        steps["create_part"] = await adapter.create_part()
        after.append(("create_part", _read(adapter)))
        steps["create_sketch"] = await adapter.create_sketch("Top")
        after.append(("create_sketch", _read(adapter)))
        first = await adapter.add_line(0, 0, 30, 0)
        second = await adapter.add_line(0, 20, 80, 20)
        after.append(("add_line", _read(adapter)))
        steps["add_sketch_dimension"] = await adapter.add_sketch_dimension(
            first.data, None, "linear", 50.0
        )
        after.append(("add_sketch_dimension", _read(adapter)))
        lengths = {
            "first_mm": _length_mm(adapter, first.data),
            "second_mm": _length_mm(adapter, second.data),
        }
        steps["exit_sketch"] = await adapter.exit_sketch()
        after.append(("exit_sketch", _read(adapter)))
        adapter._handle_com_operation = real

        # 2. A killed server's note, put back by the next connect.
        _set(adapter, dict.fromkeys(users, False))
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / f"{ended.pid}.json").write_text(json.dumps({"10": True, "642": True}))
        recovered = UserSettings(notes).recover(adapter.swApp)
        after_recover = _read(adapter)
        note_left = (notes / f"{ended.pid}.json").exists()

        # 3. The cost of a call: options on (switched each time) and off.
        timings: dict[str, list[float]] = {"options_on_ms": [], "options_off_ms": []}
        for label, values in (("options_on_ms", on), ("options_off_ms", None)):
            _set(adapter, values or dict.fromkeys(users, False))
            for _ in range(CALLS):
                result = adapter._handle_com_operation("noop", lambda: None)
                timings[label].append(result.execution_time * 1000.0)
        _set(adapter, on)
        on_after_timing = _read(adapter)
    finally:
        adapter._handle_com_operation = real
        _set(adapter, users)
    users_after = _read(adapter)

    receipt.update(
        {
            "users_options": {str(t): v for t, v in users.items()},
            "users_options_after_test": {str(t): v for t, v in users_after.items()},
            "steps": {
                name: {"status": str(r.status), "error": r.error}
                for name, r in steps.items()
            },
            "inside_calls": [
                {"call": n, "options": {str(t): v for t, v in o.items()}}
                for n, o in inside
            ],
            "after_calls": [
                {"call": n, "options": {str(t): v for t, v in o.items()}}
                for n, o in after
            ],
            "lengths": lengths,
            "recover": {
                "restored": {str(t): v for t, v in recovered.items()},
                "options_after": {str(t): v for t, v in after_recover.items()},
                "note_left": note_left,
            },
            "cost_per_call_ms": {
                label: {
                    "median": round(statistics.median(values), 3),
                    "max": round(max(values), 3),
                    "calls": len(values),
                }
                for label, values in timings.items()
            },
            "options_on_after_timed_calls": all(on_after_timing.values()),
        }
    )
    checks = {
        "every step succeeded": all(r.is_success for r in steps.values()),
        "a call saw all five off": bool(inside)
        and all(not any(o.values()) for _, o in inside),
        "after every call all five on": all(all(o.values()) for _, o in after),
        "first line dimensioned to 50": abs(lengths["first_mm"] - 50.0) < 1e-3,
        "second line still 80": abs(lengths["second_mm"] - 80.0) < 1e-3,
        "recover put back 10 and 642": recovered == {10: True, 642: True}
        and after_recover[10]
        and after_recover[642]
        and not after_recover[100],
        "recover removed the note": not note_left,
        "timed calls left them on": all(on_after_timing.values()),
        "the user's own values back": users_after == users,
    }
    receipt["checks"] = checks
    receipt["passed"] = all(checks.values())
    RECEIPT.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    assert receipt["passed"], {k: v for k, v in checks.items() if not v}
