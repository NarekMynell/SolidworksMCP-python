"""The user's SolidWorks options: off during each call, back after, on real SolidWorks.

The options automation needs off (``user_settings.AUTOMATION_OFF``) are
switched on first, as a designer who sketches by hand has them, under the
options lock and only when no other server is in a call (else the test is
skipped); the user's own values come back at the end.

1. A part, a sketch, two lines (30 and 80 mm) and a 50 mm dimension on the
   first, each through the adapter: at the start and at the end of every call
   all options are off; after it, all are on again. The dimension returned
   (no Modify box held it), the first line at 50 mm, the second still 80 mm.
2. A control: the same sketch with *Scale sketch on first dimension creation*
   taken out of the switching (left on): what it does to the second line.
3. A real second server (``user_settings_server.py``) is killed while its
   call holds the options off (it is a holder in the ledger); this server's
   next call drops it, puts the options back, and leaves no ledger.
4. Two more servers at once, their calls overlapping: every call of both
   sees the options off at its start and at its end (neither puts them back
   under the other), and after both they are on.
5. The cost of the switching per call (settle pause 0): a call that does
   nothing, 20 times with the options on, 20 times with them off.

Writes ``docs/proofs/user-settings.json`` (with the SolidWorks release and the
code's commit) before it asserts. Collected only with
``SOLIDWORKS_MCP_RUN_REAL_INTEGRATION=1`` on Windows.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).parents[2]
RECEIPT = ROOT / "docs" / "proofs" / "user-settings.json"
SERVER = Path(__file__).parent / "user_settings_server.py"
CALLS = 20


def _read(adapter: Any) -> dict[int, bool]:
    from solidworks_mcp.adapters.solidworks.user_settings import AUTOMATION_OFF

    return {t: bool(adapter.swApp.GetUserPreferenceToggle(t)) for t in AUTOMATION_OFF}


def _keys(options: dict[int, bool]) -> dict[str, bool]:
    return {str(t): v for t, v in options.items()}


def _holders(release: str | None) -> list[dict[str, Any]]:
    """The ledger's holders whose process still runs."""
    from solidworks_mcp.adapters.solidworks import user_settings

    path = user_settings.notes_folder() / f"ledger-{release or 'unknown'}.json"
    try:
        holders = json.loads(path.read_text(encoding="utf-8")).get("holders") or []
    except (OSError, ValueError):
        return []
    return [
        h
        for h in holders
        if isinstance(h.get("pid"), int)
        and user_settings.process_alive(h["pid"], h.get("started"))
    ]


def _set(adapter: Any, values: dict[int, bool], release: str | None) -> None:
    """Set the user's options as a person would, under the options lock."""
    from solidworks_mcp.adapters.solidworks import user_settings

    lock = user_settings.lock_for(user_settings.notes_folder())
    with lock.held(30) as taken:
        if not taken or _holders(release):
            pytest.skip(
                "another server's call holds the options: not the user's values now"
            )
        for toggle, value in values.items():
            adapter.swApp.SetUserPreferenceToggle(toggle, value)


def _length_mm(adapter: Any, entity_id: str) -> float:
    from solidworks_mcp.adapters import sw_type_info

    segment = sw_type_info.flagged(
        adapter._sketch_entities[entity_id], "ISketchSegment"
    )
    return round(float(segment.GetLength()) * 1000.0, 4)


async def _sketch(adapter: Any) -> tuple[dict[str, Any], dict[str, float]]:
    """A part with two lines, 30 and 80 mm, and a 50 mm dimension on the first."""
    steps = {"create_part": await adapter.create_part()}
    steps["create_sketch"] = await adapter.create_sketch("Top")
    first = await adapter.add_line(0, 0, 30, 0)
    second = await adapter.add_line(0, 20, 80, 20)
    steps["add_sketch_dimension"] = await adapter.add_sketch_dimension(
        first.data, None, "linear", 50.0
    )
    lengths = {
        "first_mm": _length_mm(adapter, first.data),
        "second_mm": _length_mm(adapter, second.data),
    }
    steps["exit_sketch"] = await adapter.exit_sketch()
    return steps, lengths


def _server(name: str, out: Path, count: int, hold: float) -> subprocess.Popen[str]:
    env = {**os.environ, "SOLIDWORKS_MCP_COM_SETTLE_SECONDS": "0"}
    env.pop("SOLIDWORKS_MCP_USER_SETTINGS_DIR", None)  # the real ledger folder
    return subprocess.Popen(
        [sys.executable, str(SERVER), name, str(out), str(count), str(hold)],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
        cwd=ROOT,
    )


def _line(server: subprocess.Popen[str]) -> str:
    assert server.stdout is not None
    return server.stdout.readline().strip()


def _commit() -> str:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "src"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError:
        return "unknown"
    return f"{head}{' + uncommitted src changes' if dirty else ''}"


async def test_options_are_off_in_each_call_and_back_after_it(
    scratch: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from solidworks_mcp.adapters.solidworks import user_settings

    adapter = scratch.adapter
    release = user_settings._version(adapter.swApp)
    users = _read(adapter)
    on = dict.fromkeys(users, True)
    inside: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    real = adapter._handle_com_operation

    def watched(name: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        def probe(*a: Any, **k: Any) -> Any:
            start = _read(adapter)
            try:
                return func(*a, **k)
            finally:
                inside.append(
                    {"call": name, "start": _keys(start), "end": _keys(_read(adapter))}
                )

        result = real(name, probe, *args, **kwargs)
        if adapter._user_settings._depth == 0:
            after.append({"call": name, "options": _keys(_read(adapter))})
        return result

    receipt: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "solidworks": str(adapter._get_attr_or_call(adapter.swApp, "RevisionNumber")),
        "commit": _commit(),
    }
    checks: dict[str, bool] = {}
    killed_pid: int | None = None
    try:
        _set(adapter, on, release)
        # 1. Calls of this server.
        adapter._handle_com_operation = watched
        steps, lengths = await _sketch(adapter)
        adapter._handle_com_operation = real

        # 2. The control: 642 left on through a dimension.
        with monkeypatch.context() as patch:
            patch.delitem(user_settings.AUTOMATION_OFF, 642)
            control_steps, control = await _sketch(adapter)
        _set(adapter, on, release)

        # 3. A real server killed while its call holds the options off.
        killed = _server("killed", tmp_path / "killed.json", 1, 120.0)
        try:
            ready = _line(killed).split()
            killed_pid = int(ready[2]) if len(ready) == 3 else None
            entered = _line(killed) == "inside killed"
            held_by = [h["pid"] for h in _holders(release)]
            while_held = _read(adapter)
            killed.kill()
            killed.wait(30)
            time.sleep(1.0)  # the interpreter behind the venv launcher ends with it
        finally:
            if killed.poll() is None:
                killed.kill()
        next_call = adapter._handle_com_operation(
            "after_the_kill", lambda: _read(adapter)
        )
        after_kill = _read(adapter)
        held_after = [h["pid"] for h in _holders(release)]

        # 4. Two servers at once.
        outs = [tmp_path / "a.json", tmp_path / "b.json"]
        both = [_server(n, o, 4, 0.5) for n, o in zip(("a", "b"), outs, strict=True)]
        for server in both:
            server.wait(240)
        rows = [
            r
            for out in outs
            if out.is_file()  # a server that failed wrote nothing: too few calls
            for r in json.loads(out.read_text(encoding="utf-8"))
        ]
        spans = sorted((r["start"], r["end"]) for r in rows if "start" in r)
        overlapping = sum(
            1 for x, y in zip(spans, spans[1:], strict=False) if y[0] < x[1]
        )
        after_both = _read(adapter)

        # 5. The cost per call, without the settle pause.
        monkeypatch.setenv("SOLIDWORKS_MCP_COM_SETTLE_SECONDS", "0")
        timings: dict[str, list[float]] = {"options_on_ms": [], "options_off_ms": []}
        for label, values in (
            ("options_on_ms", on),
            ("options_off_ms", dict.fromkeys(on, False)),
        ):
            _set(adapter, values, release)
            for _ in range(CALLS):
                started = time.perf_counter()
                adapter._handle_com_operation("noop", lambda: None)
                timings[label].append((time.perf_counter() - started) * 1000.0)
        _set(adapter, on, release)
        on_after_timing = _read(adapter)

        receipt.update(
            {
                "users_options": _keys(users),
                "steps": {
                    n: {"status": str(r.status), "error": r.error}
                    for n, r in steps.items()
                },
                "inside_calls": inside,
                "after_calls": after,
                "lengths": lengths,
                "control_642_left_on": {
                    "steps": {n: str(r.status) for n, r in control_steps.items()},
                    "lengths": control,
                    "second_line_rescaled": abs(control["second_mm"] - 80.0) > 1e-3,
                },
                "killed_server": {
                    "pid": killed_pid,
                    "entered_its_call": entered,
                    "holders_while_its_call_held": held_by,
                    "options_while_its_call_held": _keys(while_held),
                    "next_call": str(next_call.status),
                    "options_after": _keys(after_kill),
                    "holders_after": held_after,
                },
                "two_servers": {
                    "calls": len(spans),
                    "overlapping_calls": overlapping,
                    "rows": rows,
                    "exit_codes": [s.returncode for s in both],
                    "options_after": _keys(after_both),
                },
                "cost_per_call_ms": {
                    label: {
                        "median": round(statistics.median(values), 3),
                        "max": round(max(values), 3),
                        "calls": len(values),
                    }
                    for label, values in timings.items()
                },
            }
        )
        checks = {
            "every step succeeded": all(r.is_success for r in steps.values()),
            "off at the start and end of every call": bool(inside)
            and all(
                not any(c["start"].values()) and not any(c["end"].values())
                for c in inside
            ),
            "on after every call": bool(after)
            and all(all(c["options"].values()) for c in after),
            "first line dimensioned to 50": abs(lengths["first_mm"] - 50.0) < 1e-3,
            "second line still 80": abs(lengths["second_mm"] - 80.0) < 1e-3,
            "the killed server held them off": entered
            and killed_pid in held_by
            and not any(while_held.values()),
            "the next call put them back": next_call.is_success
            and all(after_kill.values())
            and killed_pid not in held_after,
            "two servers ran 8 calls": len(spans) == 8
            and all(c == 0 for c in (s.returncode for s in both)),
            "every call of both saw them off at start and end": len(rows) == 8
            and all(
                not any(r["at_start"].values()) and not any(r["at_end"].values())
                for r in rows
            ),
            "on after both servers": all(after_both.values()),
            "timed calls left them on": all(on_after_timing.values()),
        }
    finally:
        adapter._handle_com_operation = real
        try:
            _set(adapter, users, release)
        finally:
            receipt["users_options_after_test"] = _keys(_read(adapter))
            checks["the user's own values back"] = receipt[
                "users_options_after_test"
            ] == _keys(users)
            receipt["checks"] = checks
            receipt["passed"] = bool(checks) and all(checks.values())
            RECEIPT.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    assert receipt["passed"], {k: v for k, v in checks.items() if not v}
