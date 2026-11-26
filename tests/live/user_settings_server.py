"""A second server for ``test_live_sw_user_settings``: connects, runs held operations, reports.

Usage: python user_settings_server.py NAME OUT COUNT HOLD_SECONDS

Prints ``ready NAME PID`` once connected (PID is this interpreter's: a venv's
python.exe is a launcher with another id). Each of COUNT operations reads the
options at its start, prints ``inside NAME``, holds HOLD_SECONDS and reads
them again; OUT gets one row per operation. A test kills it while an
operation holds, or runs two at once.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


async def main(name: str, out: Path, count: int, hold: float) -> None:
    from solidworks_mcp.adapters.pywin32_adapter import PyWin32Adapter
    from solidworks_mcp.adapters.solidworks.user_settings import AUTOMATION_OFF

    adapter = PyWin32Adapter({})
    await adapter.connect()
    print(f"ready {name} {os.getpid()}", flush=True)
    rows: list[dict[str, Any]] = []

    def options() -> dict[str, bool]:
        return {
            str(t): bool(adapter.swApp.GetUserPreferenceToggle(t))
            for t in AUTOMATION_OFF
        }

    try:
        for index in range(count):

            def held() -> dict[str, Any]:
                start, at_start = time.time(), options()
                print(f"inside {name}", flush=True)
                time.sleep(hold)
                return {
                    "start": start,
                    "end": time.time(),
                    "at_start": at_start,
                    "at_end": options(),
                }

            result = adapter._handle_com_operation(f"{name}-{index}", held)
            rows.append(result.data or {"error": result.error})
        out.write_text(json.dumps(rows), encoding="utf-8")
    finally:
        await adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(
        main(sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]))
    )
