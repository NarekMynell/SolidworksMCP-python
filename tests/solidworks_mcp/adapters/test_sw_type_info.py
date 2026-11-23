"""Tests for sw_type_info method flagging helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from solidworks_mcp.adapters import sw_type_info


def test_import_handles_missing_pywin32(monkeypatch) -> None:
    """Import should handle missing win32com gracefully."""
    # Load the module under an alias while forcing win32com to fail.
    import builtins

    original_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name.startswith("win32com"):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    module_path = (
        Path(__file__).parents[3]
        / "src"
        / "solidworks_mcp"
        / "adapters"
        / "sw_type_info.py"
    )
    spec = importlib.util.spec_from_file_location(
        "sw_type_info_no_pywin32", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.PYWIN32_AVAILABLE is False


def test_interface_method_names_empty_when_unloaded(monkeypatch) -> None:
    """interface_method_names should return empty set when wrapper missing."""
    # Force the module to behave as if pywin32 is unavailable.
    from solidworks_mcp.adapters import sw_type_info

    monkeypatch.setattr(sw_type_info, "PYWIN32_AVAILABLE", False)
    sw_type_info._wrapper_module = None
    sw_type_info._interface_methods.clear()

    result = sw_type_info.interface_method_names("ISldWorks")
    assert result == frozenset()


def test_flag_methods_returns_zero_when_no_methods(monkeypatch) -> None:
    """flag_methods should no-op when no interface methods are loaded."""
    # Ensure we return zero when nothing is loaded to flag.
    from solidworks_mcp.adapters import sw_type_info

    monkeypatch.setattr(sw_type_info, "_interface_methods", {})
    assert sw_type_info.flag_methods(object(), "ISldWorks") == 0


def test_flagged_passes_through_none() -> None:
    """flagged should return None when obj is None."""
    # Validate the None short-circuit in flagged().
    from solidworks_mcp.adapters import sw_type_info

    assert sw_type_info.flagged(None, "ISldWorks") is None


def test_invalidate_flag_cache_clears_and_pops() -> None:
    """invalidate_flag_cache should clear or remove entries."""
    # Validate both the full clear and per-object pop behavior.
    from solidworks_mcp.adapters import sw_type_info

    obj = object()
    sw_type_info._flag_cache[id(obj)] = {"ISldWorks"}
    sw_type_info.invalidate_flag_cache(obj)
    assert id(obj) not in sw_type_info._flag_cache

    sw_type_info._flag_cache[id(obj)] = {"ISldWorks"}
    sw_type_info.invalidate_flag_cache()
    assert sw_type_info._flag_cache == {}


class _FakeDispatch:
    """Minimal test double mimicking a pywin32 ``CDispatch``'s flag surface.

    Records every name it was asked to flag. ``unflaggable`` simulates names
    that don't belong to the object's real COM interface — pywin32's
    ``_FlagAsMethod`` raises for those, and ``flag_members`` must skip them
    silently rather than propagate.
    """

    def __init__(self, unflaggable: set[str] | None = None) -> None:
        self.flagged_names: set[str] = set()
        self._unflaggable = unflaggable or set()

    def _FlagAsMethod(self, name: str) -> None:
        if name in self._unflaggable:
            raise Exception(f"Unknown name: {name}")
        self.flagged_names.add(name)


def test_flag_members_flags_only_requested_names() -> None:
    """flag_members should flag exactly the names passed in, nothing else."""
    from solidworks_mcp.adapters import sw_type_info

    obj = _FakeDispatch()
    count = sw_type_info.flag_members(obj, "GetNextFeature", "GetTypeName2")

    assert count == 2
    assert obj.flagged_names == {"GetNextFeature", "GetTypeName2"}


def test_flag_members_tolerates_none_object() -> None:
    """flag_members should no-op and return 0 when obj is None."""
    from solidworks_mcp.adapters import sw_type_info

    assert sw_type_info.flag_members(None, "GetNextFeature") == 0


def test_flag_members_tolerates_names_not_on_interface() -> None:
    """flag_members should skip names _FlagAsMethod rejects, not raise."""
    from solidworks_mcp.adapters import sw_type_info

    obj = _FakeDispatch(unflaggable={"NotOnInterface"})
    count = sw_type_info.flag_members(obj, "GetNextFeature", "NotOnInterface")

    assert count == 1
    assert obj.flagged_names == {"GetNextFeature"}


def test_flag_members_works_when_pywin32_unavailable(monkeypatch) -> None:
    """flag_members should still flag members when pywin32 wasn't importable.

    Unlike ``flag_methods``, ``flag_members`` doesn't consult the gen_py
    wrapper or ``_interface_methods`` — it flags exactly the names it's
    given directly on the object. It must keep doing that even in the
    no-pywin32 fallback state exercised by
    ``test_import_handles_missing_pywin32``.
    """
    import builtins

    original_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name.startswith("win32com"):
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    module_path = (
        Path(__file__).parents[3]
        / "src"
        / "solidworks_mcp"
        / "adapters"
        / "sw_type_info.py"
    )
    spec = importlib.util.spec_from_file_location(
        "sw_type_info_no_pywin32_flag_members", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.PYWIN32_AVAILABLE is False

    obj = _FakeDispatch()
    count = module.flag_members(obj, "GetNextFeature")

    assert count == 1
    assert obj.flagged_names == {"GetNextFeature"}


def test_load_wrapper_warns_when_genpy_missing(monkeypatch) -> None:
    """_load_wrapper should warn when gen_py is unavailable."""
    # Simulate gencache failing to load or generate a wrapper module.
    from solidworks_mcp.adapters import sw_type_info

    warnings: list[str] = []

    class _FakeCache:
        @staticmethod
        def GetModuleForTypelib(*_a, **_kw):
            return None

        @staticmethod
        def EnsureModule(*_a, **_kw):
            raise RuntimeError("no gen_py")

    monkeypatch.setattr(sw_type_info, "PYWIN32_AVAILABLE", True)
    monkeypatch.setattr(sw_type_info, "gencache", _FakeCache, raising=False)
    monkeypatch.setattr(
        sw_type_info,
        "logger",
        SimpleNamespace(warning=lambda msg: warnings.append(msg)),
    )
    sw_type_info._wrapper_module = None
    sw_type_info._interface_methods.clear()

    sw_type_info._load_wrapper()

    assert sw_type_info._wrapper_module is None
    assert any("gen_py wrapper not available" in msg for msg in warnings)


def test_flag_methods_ignores_none() -> None:
    """SolidWorks returned Nothing: nothing to flag."""
    from solidworks_mcp.adapters import sw_type_info

    assert sw_type_info.flag_methods(None, "ISldWorks") == 0


def test_load_wrapper_generates_the_wrapper_when_not_cached(monkeypatch) -> None:
    """No cached gen_py module -> EnsureModule generates one, then it is used."""
    from types import SimpleNamespace

    from solidworks_mcp.adapters import sw_type_info

    generated = SimpleNamespace(__name__="fake_sw_wrapper")
    state = {"ensured": False}

    def _get_module(_iid, _lcid, major, _minor):
        return generated if state["ensured"] and major == 34 else None

    def _ensure_module(_iid, _lcid, major, _minor):
        state["ensured"] = major == 34

    monkeypatch.setattr(
        sw_type_info,
        "gencache",
        SimpleNamespace(GetModuleForTypelib=_get_module, EnsureModule=_ensure_module),
        raising=False,
    )
    # DispatchBaseClass only exists when pywin32 imports (not on Linux CI).
    monkeypatch.setattr(
        sw_type_info, "DispatchBaseClass", type("Dispatch", (), {}), raising=False
    )
    monkeypatch.setattr(sw_type_info, "PYWIN32_AVAILABLE", True)
    monkeypatch.setattr(sw_type_info, "_wrapper_module", None)
    monkeypatch.setattr(sw_type_info, "_interface_methods", {})

    sw_type_info._load_wrapper()

    assert sw_type_info._wrapper_module is generated


def test_all_interface_methods_and_library_name_when_loaded(monkeypatch) -> None:
    """The public reads return a copy of the loaded names and the wrapper name."""
    from types import SimpleNamespace

    from solidworks_mcp.adapters import sw_type_info

    loaded = {"IFeature": frozenset({"GetNextFeature"})}
    monkeypatch.setattr(sw_type_info, "_interface_methods", loaded)
    monkeypatch.setattr(
        sw_type_info, "_wrapper_module", SimpleNamespace(__name__="gen_py.x0x34x0")
    )

    methods = sw_type_info.all_interface_methods()

    assert methods == loaded and methods is not loaded
    assert sw_type_info.type_library_name() == "gen_py.x0x34x0"


def test_library_name_is_none_without_the_wrapper(monkeypatch) -> None:
    """With no wrapper and no pywin32, both reads are empty."""
    from solidworks_mcp.adapters import sw_type_info

    monkeypatch.setattr(sw_type_info, "PYWIN32_AVAILABLE", False)
    monkeypatch.setattr(sw_type_info, "_wrapper_module", None)
    monkeypatch.setattr(sw_type_info, "_interface_methods", {})

    assert sw_type_info.type_library_name() is None
    assert sw_type_info.all_interface_methods() == {}


class _CountingIDispatch:
    """A raw IDispatch: fixed DISPIDs, and a count of the lookups asked."""

    def __init__(self, dispids: dict[str, int]) -> None:
        self.dispids = dispids
        self.asked: list[str] = []

    def GetIDsOfNames(self, _lcid: int, name: str) -> int:
        self.asked.append(name)
        if name not in self.dispids:
            raise Exception("Unknown name.")
        return self.dispids[name]


def _dispatch(raw: _CountingIDispatch) -> object:
    """A real pywin32 ``CDispatch`` around ``raw``."""
    dynamic = pytest.importorskip("win32com.client.dynamic")
    build = pytest.importorskip("win32com.client.build")
    return dynamic.CDispatch(raw, build.DispatchItem(), "Fake")


@pytest.fixture
def one_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    """The type library holds one interface of three methods."""
    monkeypatch.setattr(sw_type_info, "_wrapper_module", object())
    monkeypatch.setattr(
        sw_type_info, "_interface_methods", {"IThing": frozenset({"A", "B", "C"})}
    )
    monkeypatch.setattr(sw_type_info, "_flag_cache", {})
    monkeypatch.setattr(sw_type_info, "_dispid_cache", {})


def test_dispids_are_learnt_once_per_interface(one_interface) -> None:
    """The second dispatch is checked with two lookups, not one per method."""
    first_raw = _CountingIDispatch({"A": 1, "B": 2, "C": 3})
    second_raw = _CountingIDispatch({"A": 1, "B": 2, "C": 3})
    first, second = _dispatch(first_raw), _dispatch(second_raw)

    assert sw_type_info.flag_methods(first, "IThing") == 3
    assert sw_type_info.flag_methods(second, "IThing") == 3

    assert sorted(first_raw.asked) == ["A", "B", "C"]
    assert sorted(second_raw.asked) == ["A", "C"]
    entries = second._olerepr_.mapFuncs  # type: ignore[attr-defined]
    assert {name: entries[name].dispid for name in "ABC"} == {"A": 1, "B": 2, "C": 3}


def test_a_dispatch_laid_out_differently_is_looked_up_in_full(one_interface) -> None:
    first = _dispatch(_CountingIDispatch({"A": 1, "B": 2, "C": 3}))
    other_raw = _CountingIDispatch({"A": 10, "B": 20})
    other = _dispatch(other_raw)

    sw_type_info.flag_methods(first, "IThing")
    flagged = sw_type_info.flag_methods(other, "IThing")

    assert flagged == 2
    entries = other._olerepr_.mapFuncs  # type: ignore[attr-defined]
    assert (entries["A"].dispid, entries["B"].dispid) == (10, 20)
    assert "C" not in entries
    # The first object's numbers stay the cache's.
    assert sw_type_info._dispid_cache["IThing"] == {"A": 1, "B": 2, "C": 3}


def test_a_dispatch_that_refuses_the_check_is_looked_up_in_full(one_interface) -> None:
    sw_type_info.flag_methods(_dispatch(_CountingIDispatch({"A": 1, "C": 3})), "IThing")
    raw = _CountingIDispatch({"B": 2})
    later = _dispatch(raw)

    assert sw_type_info.flag_methods(later, "IThing") == 1
    assert later._olerepr_.mapFuncs["B"].dispid == 2  # type: ignore[attr-defined]
