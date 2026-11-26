"""Tests for ``build_weldment_profiles``, through the mock adapter."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from solidworks_mcp.adapters.base import AdapterResult, AdapterResultStatus
from solidworks_mcp.adapters.mock_weldments import MOCK_PROFILE_HEADER
from solidworks_mcp.tools import weldments
from solidworks_mcp.tools.support_levels import SUPPORT_LEVELS
from solidworks_mcp.tools.weldments import (
    BuildWeldmentProfilesInput,
    load_profile_spec,
    register_weldment_tools,
    results_path,
)

FIXTURE = (
    Path(__file__).parents[2]
    / "live"
    / "data"
    / "weldment_profiles"
    / "opus_profiles.json"
)
#: Every successful reply ends with the tool's support level.
BETA = SUPPORT_LEVELS["build_weldment_profiles"].warning()


def _catalog() -> list[dict[str, Any]]:
    return list(json.loads(FIXTURE.read_text(encoding="utf-8"))["profiles"])


async def _tools(mcp_server: Any, adapter: Any) -> dict[str, Any]:
    await register_weldment_tools(mcp_server, adapter, {})
    return {t.name: t.fn for t in await mcp_server.list_tools()}


def _spec(tmp_path: Path, profiles: list[Any], name: str = "spec.json") -> str:
    path = tmp_path / name
    path.write_text(
        json.dumps({"profiles": profiles}, ensure_ascii=False), encoding="utf-8"
    )
    return str(path)


def _results(spec: str) -> dict[str, Any]:
    return dict(json.loads(Path(results_path(spec)).read_text(encoding="utf-8")))


def test_input_refusals() -> None:
    with pytest.raises(ValidationError):
        BuildWeldmentProfilesInput(spec_file="")
    with pytest.raises(ValidationError):
        BuildWeldmentProfilesInput(spec_file="s.json", only=[])
    with pytest.raises(ValidationError):
        BuildWeldmentProfilesInput(spec_file="s.json", define="all")
    plain = BuildWeldmentProfilesInput(spec_file="s.json")
    assert (plain.root, plain.overwrite, plain.only, plain.define) == (
        None,
        False,
        None,
        "dimensions",
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "doesn't exist"),
        ("{not json", "isn't readable JSON"),
        ('{"sizes": []}', 'must hold a list of profiles, or {"profiles": [...]}'),
        ("[]", 'must hold a list of profiles, or {"profiles": [...]}'),
    ],
)
def test_unreadable_specs_are_refused(
    tmp_path: Path, content: str | None, message: str
) -> None:
    path = tmp_path / "spec.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(weldments.InvalidInputError, match=re.escape(message)):
        load_profile_spec(str(path))


def test_a_spec_may_be_a_bare_list(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(_catalog()[:2]), encoding="utf-8")
    assert len(load_profile_spec(str(path))) == 2


@pytest.mark.parametrize(
    ("spec", "results"),
    [
        ("C:/c/opus.json", "C:/c/opus.results.json"),
        ("C:/c/opus.JSON", "C:/c/opus.results.json"),
        ("C:/c/opus.txt", "C:/c/opus.txt.results.json"),
    ],
)
def test_the_results_file_sits_beside_the_spec(spec: str, results: str) -> None:
    assert results_path(spec) == results


@pytest.mark.asyncio
async def test_a_catalog_is_built_and_its_results_written(
    mcp_server: Any, mock_adapter: Any, tmp_path: Path
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    spec = _spec(tmp_path, _catalog())
    root = tmp_path / "Weldment Profiles"

    reply = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": str(root)}
    )

    assert reply["status"] == "success", reply
    assert reply["message"] == (
        f"Built 11, skipped 0, failed 0 of 11 weldment profile(s) under {root}"
    )
    assert reply["warning"] == BETA  # nothing of its own to say
    profiles = reply["profiles"]
    assert (profiles["built"], profiles["skipped"], profiles["failed"]) == (11, 0, 0)
    assert profiles["root"] == str(root)
    assert profiles["results_file"] == str(tmp_path / "spec.results.json")
    first = profiles["items"][0]
    assert set(first) == {"path", "status", "error", "seconds", "sha256"}
    file = root / "Opus" / "rhs-welded" / "80x60x2.7.sldlfp"
    assert file.read_bytes().startswith(MOCK_PROFILE_HEADER)
    assert first["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    channel = root / "Opus" / "channels" / "[10У.sldlfp"
    assert channel.is_file()

    results = _results(spec)
    assert results["spec_file"] == spec and results["root"] == str(root)
    assert (results["built"], results["skipped"], results["failed"]) == (11, 0, 0)
    row = results["profiles"][0]
    assert row["file"] == str(file)
    assert row["designation"] == "80×60×2.7"
    # What the spec says of the section, so an upload needs only this file.
    assert row["shape"] == "rect_tube"
    assert row["dims"] == _catalog()[0]["dims"]
    assert row["properties"]["Opus code"] == "rhs-welded/80x60x2.7"
    assert row["area_mm2"] == pytest.approx(708.0666)
    assert row["expected_area_mm2"] == 708.0666
    assert (row["definition"], row["define"]) == ("fully defined", "dimensions")
    assert row["bytes"] == file.stat().st_size
    assert row["at"] == results["written"]


@pytest.mark.asyncio
async def test_bad_entries_fail_alone(
    mcp_server: Any, mock_adapter: Any, tmp_path: Path
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    good, gap, flat, extra, unnamed = (copy.deepcopy(_catalog()[0]) for _ in range(5))
    gap["path"] = "Opus/x/gap.sldlfp"
    gap["outline"]["outer"][1]["b"] = [24.6, -39.0]
    flat["path"] = "Opus/rhs-welded.sldlfp"
    extra["path"] = "Opus/x/extra.sldlfp"
    extra["grade"] = "S235"
    extra["area_mm2"] = "big"
    unnamed["path"] = "Opus/x/unnamed.sldlfp"
    unnamed["properties"] = {" ": "x"}
    spec = _spec(tmp_path, [good, gap, flat, "not a profile", extra, unnamed])

    reply = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": str(tmp_path / "root")}
    )

    assert reply["status"] == "success", reply
    items = reply["profiles"]["items"]
    assert [i["status"] for i in items] == ["built"] + ["error"] * 5
    errors = [i["error"] for i in items[1:]]
    assert errors[0].startswith("outline: outer[1] ends at [24.6, -39.0] but outer[2]")
    assert "one file per size" in errors[1]
    assert items[3]["path"] == "profiles[3]"
    assert "Input should be a valid dictionary" in errors[2]
    # Every problem of an entry, not the first only.
    assert "area_mm2: Input should be a valid number" in errors[3]
    assert "grade: Extra inputs are not permitted" in errors[3]
    assert "every property needs a name" in errors[4]
    assert reply["warning"].startswith("5 profile(s) failed: Opus/x/gap.sldlfp: ")
    assert reply["warning"].endswith(f". {BETA}")


@pytest.mark.asyncio
async def test_two_entries_for_one_path_are_both_refused(
    mcp_server: Any, mock_adapter: Any, tmp_path: Path
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    first, again = _catalog()[0], copy.deepcopy(_catalog()[0])
    again["path"] = again["path"].upper().replace("/", "\\")
    spec = _spec(tmp_path, [first, again, _catalog()[1]])

    reply = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": str(tmp_path / "root")}
    )

    items = reply["profiles"]["items"]
    assert [i["status"] for i in items] == ["error", "error", "built"]
    assert "profiles[0], profiles[1] all go to" in items[0]["error"]


@pytest.mark.asyncio
async def test_only_skip_and_overwrite(
    mcp_server: Any, mock_adapter: Any, tmp_path: Path
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    spec = _spec(tmp_path, _catalog())
    root = str(tmp_path / "root")
    only = ["opus\\ANGLES\\l63x40x5.SLDLFP", "Opus/tees/T25x25x3.sldlfp"]

    first = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": root, "only": only}
    )
    again = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": root, "only": only}
    )
    redone = await tools["build_weldment_profiles"](
        {
            "spec_file": spec,
            "root": root,
            "only": only[:1],
            "overwrite": True,
            "define": "none",
        }
    )
    unknown = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": root, "only": ["Opus/angles/L99.sldlfp"]}
    )

    assert [i["path"] for i in first["profiles"]["items"]] == [
        "Opus/angles/L63x40x5.sldlfp",
        "Opus/tees/T25x25x3.sldlfp",
    ]
    assert [i["status"] for i in again["profiles"]["items"]] == ["skipped"] * 2
    assert [i["sha256"] for i in again["profiles"]["items"]] == [
        i["sha256"] for i in first["profiles"]["items"]
    ]
    assert again["message"].startswith("Built 0, skipped 2, failed 0 of 2")
    assert redone["profiles"]["items"][0]["status"] == "built"
    assert redone["warning"] == BETA  # define none asks for no full definition
    assert _results(spec)["profiles"][0]["definition"] == "under defined"
    assert unknown["error_type"] == "invalid_input"
    assert "only names 1 path(s) the spec doesn't have" in unknown["message"]


@pytest.mark.asyncio
async def test_a_spec_built_in_parts_keeps_one_results_file(
    mcp_server: Any, mock_adapter: Any, tmp_path: Path
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    catalog = _catalog()[:4]
    paths = [p["path"] for p in catalog]
    spec = _spec(tmp_path, catalog)
    root = str(tmp_path / "root")

    await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": root, "only": [paths[3], paths[0]]}
    )
    first = _results(spec)
    await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": root, "only": [paths[2]]}
    )
    second = _results(spec)
    # A profile taken out of the spec drops out of its results.
    _spec(tmp_path, catalog[:3])
    await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": root, "only": [paths[1]]}
    )
    third = _results(spec)

    # In spec order, whatever order only gave.
    assert [p["path"] for p in first["profiles"]] == [paths[0], paths[3]]
    assert [p["path"] for p in second["profiles"]] == [paths[0], paths[2], paths[3]]
    assert second["profiles"][0] == first["profiles"][0]  # kept as it was
    assert (second["built"], second["failed"]) == (3, 0)
    assert [p["path"] for p in third["profiles"]] == paths[:3]


@pytest.mark.asyncio
async def test_an_unreadable_old_results_file_is_replaced(
    mcp_server: Any, mock_adapter: Any, tmp_path: Path
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    spec = _spec(tmp_path, _catalog()[:2])
    Path(results_path(spec)).write_text("{half", encoding="utf-8")

    reply = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": str(tmp_path / "root")}
    )

    assert reply["status"] == "success"
    assert reply["warning"].startswith("the old results file couldn't be read (")
    assert "it now lists this call's profiles only." in reply["warning"]
    assert len(_results(spec)["profiles"]) == 2


@pytest.mark.asyncio
async def test_nothing_built_is_an_error_with_every_profile(
    mcp_server: Any, mock_adapter: Any, tmp_path: Path
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    blocked = tmp_path / "root"
    blocked.write_text("a file where the root folder should be", encoding="utf-8")
    spec = _spec(tmp_path, _catalog()[:2])

    reply = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": str(blocked)}
    )
    refused = await tools["build_weldment_profiles"](
        {"spec_file": _spec(tmp_path, [{"path": "a.sldlfp"}], "bad.json")}
    )

    assert reply["status"] == "error"
    assert reply["error_type"] == "profiles_not_built"
    assert reply["message"] == (
        "No weldment profile was built: 2 failed; each one's error is in "
        "data.profiles.items"
    )
    assert "data.profiles.items" in reply["hint"]
    assert [i["status"] for i in reply["data"]["profiles"]["items"]] == ["error"] * 2
    assert len(_results(spec)["profiles"]) == 2
    assert refused["error_type"] == "profiles_not_built"
    assert refused["data"]["profiles"]["root"] is None


def _lost_adapter(
    monkeypatch: pytest.MonkeyPatch, adapter: Any, built_first: bool
) -> None:
    """The adapter's reply when SolidWorks went away during the build."""
    gone = "(-2147023174, 'The RPC server is unavailable.', None, None)"

    async def lost(params: dict[str, Any]) -> AdapterResult[dict[str, Any]]:
        items = []
        for index, profile in enumerate(params["profiles"]):
            built = built_first and index == 0
            items.append(
                {
                    "path": profile["path"],
                    "file": None,
                    "status": "built" if built else "error",
                    "error": None
                    if built
                    else gone
                    if index == int(built_first)
                    else "not built: SolidWorks stopped answering at ...",
                    "seconds": 1.0,
                    "sha256": "0" * 64 if built else None,
                    "bytes": 10 if built else None,
                    "definition": "fully defined" if built else None,
                }
            )
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={"root": params["root"], "items": items},
        )

    monkeypatch.setattr(adapter, "build_weldment_profiles", lost)


@pytest.mark.asyncio
async def test_a_lost_solidworks_says_so(
    mcp_server: Any,
    mock_adapter: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lost_adapter(monkeypatch, mock_adapter, built_first=False)
    tools = await _tools(mcp_server, mock_adapter)
    spec = _spec(tmp_path, _catalog()[:3])

    reply = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": str(tmp_path)}
    )

    assert reply["error_type"] == "solidworks_unavailable"
    assert reply["message"].startswith(
        "SolidWorks stopped answering while the profiles were built: (-2147023174"
    )
    assert len(_results(spec)["profiles"]) == 3


@pytest.mark.asyncio
async def test_a_solidworks_lost_part_way_is_the_first_warning(
    mcp_server: Any,
    mock_adapter: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lost_adapter(monkeypatch, mock_adapter, built_first=True)
    tools = await _tools(mcp_server, mock_adapter)
    spec = _spec(tmp_path, _catalog()[:3])

    reply = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": str(tmp_path)}
    )

    assert reply["status"] == "success"
    assert reply["warning"].startswith(
        "SolidWorks stopped answering at Opus/chs-welded/57x3.5.sldlfp: restart it"
    )
    assert "2 profile(s) failed" in reply["warning"]


@pytest.mark.asyncio
async def test_outlines_that_disagree_with_their_area_or_mass_are_named(
    mcp_server: Any, mock_adapter: Any, tmp_path: Path
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    area, mass, close = (copy.deepcopy(_catalog()[i]) for i in (0, 1, 2))
    area["area_mm2"] *= 1.01
    mass["mass_per_m"] *= 0.99
    close["area_mm2"] *= 1.001  # within 0.2 %
    spec = _spec(tmp_path, [area, mass, close])

    reply = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": str(tmp_path / "root")}
    )

    items = reply["profiles"]["items"]
    assert "area_mm2 says 715.147 (-0.99 %)" in items[0]["warning"]
    assert "mass_per_m says 4.57172 (+1.01 %)" in items[1]["warning"]
    assert "warning" not in items[2]
    assert reply["warning"].startswith(
        "2 outline(s) disagree with their area_mm2 or mass_per_m by more than 0.2 %"
    )


@pytest.mark.asyncio
async def test_a_results_file_that_cannot_be_written_is_a_warning(
    mcp_server: Any, mock_adapter: Any, tmp_path: Path
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    spec = _spec(tmp_path, _catalog()[:1])
    Path(results_path(spec)).mkdir()  # a folder where the file should go

    reply = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": str(tmp_path / "root")}
    )

    assert reply["status"] == "success"
    assert reply["profiles"]["built"] == 1
    assert reply["profiles"]["results_file"] is None
    assert reply["warning"].startswith("the results file couldn't be written: ")


@pytest.mark.asyncio
async def test_sketches_left_under_defined_are_named(
    mcp_server: Any,
    mock_adapter: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def loose(params: dict[str, Any]) -> AdapterResult[dict[str, Any]]:
        items = [
            {
                "path": p["path"],
                "file": None,
                "status": "built",
                "error": None,
                "seconds": 1.5,
                "sha256": "0" * 64,
                "bytes": 10,
                "definition": "under defined",
            }
            for p in params["profiles"]
        ]
        return AdapterResult(
            status=AdapterResultStatus.SUCCESS,
            data={"root": params["root"], "items": items},
            execution_time=2.0,
        )

    monkeypatch.setattr(mock_adapter, "build_weldment_profiles", loose)
    tools = await _tools(mcp_server, mock_adapter)
    spec = _spec(tmp_path, _catalog()[:2])

    reply = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": str(tmp_path)}
    )

    assert reply["warning"] == (
        "2 sketch(es) Fully Define Sketch left under defined (the profiles work "
        "the same): Opus/rhs-welded/80x60x2.7.sldlfp, Opus/chs-welded/57x3.5.sldlfp. "
        f"{BETA}"
    )
    assert reply["profiles"]["seconds"] == 3.0
    assert reply["execution_time"] == 2.0


@pytest.mark.asyncio
async def test_too_many_profiles_and_no_root(
    mcp_server: Any,
    mock_adapter: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = await _tools(mcp_server, mock_adapter)
    spec = _spec(tmp_path, _catalog()[:3])
    monkeypatch.setattr(weldments, "MAX_PROFILES", 2)

    many = await tools["build_weldment_profiles"](
        {"spec_file": spec, "root": str(tmp_path)}
    )
    monkeypatch.setattr(weldments, "MAX_PROFILES", 2000)
    rootless = await tools["build_weldment_profiles"]({"spec_file": spec})

    assert many["error_type"] == "invalid_input"
    assert "3 profiles in one call; it takes 2" in many["message"]
    assert rootless["status"] == "error"
    assert "has no Weldment Profiles folder setting: give root" in rootless["message"]


@pytest.mark.asyncio
async def test_an_adapter_without_the_build(mcp_server: Any, tmp_path: Path) -> None:
    tools = await _tools(mcp_server, SimpleNamespace())
    spec = _spec(tmp_path, _catalog()[:1])

    reply = await tools["build_weldment_profiles"]({"spec_file": spec})

    assert reply["error_type"] == "not_supported"
