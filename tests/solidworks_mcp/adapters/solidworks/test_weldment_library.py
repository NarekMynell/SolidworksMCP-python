"""Tests for the weldment profile library and the member grouping (no COM)."""

from __future__ import annotations

from pathlib import Path

import pytest

from solidworks_mcp.adapters.solidworks.weldment_library import (
    ProfileLibrary,
    chains,
    normalise,
)

#: The sizes each profile file holds, as GetConfigurationNames gives them.
_SIZES = {
    "ipe.sldlfp": ["IPE100", "IPE200", "IPE300"],
    "hea.sldlfp": ["HE300A", "HE400B", "HE400M"],
    "shs.sldlfp": ["RHS100x100x5", "RHS250x250x12.5"],
}


@pytest.fixture
def library(tmp_path: Path) -> ProfileLibrary:
    """DIN and BSI as files with sizes, ansi as an old folder-per-type library."""
    (tmp_path / "DIN").mkdir()
    (tmp_path / "DIN" / "ipe.sldlfp").write_bytes(b"")
    (tmp_path / "DIN" / "hea.sldlfp").write_bytes(b"")
    (tmp_path / "BSI").mkdir()
    (tmp_path / "BSI" / "shs.sldlfp").write_bytes(b"")
    old = tmp_path / "ansi inch" / "c channel"
    old.mkdir(parents=True)
    (old / "3 x 5.sldlfp").write_bytes(b"")
    (old / "4 x 5.4.sldlfp").write_bytes(b"")
    (tmp_path / "empty").mkdir()
    asked: list[str] = []

    def sizes_of(path: str) -> list[str]:
        asked.append(path)
        return _SIZES[Path(path).name]

    found = ProfileLibrary([str(tmp_path), str(tmp_path / "missing")], sizes_of)
    found.asked = asked  # type: ignore[attr-defined]
    return found


@pytest.mark.parametrize(
    ("text", "reduced"),
    [
        ("SHS 250 × 250 × 12.5", "shs250x250x12.5"),
        ("HEB400", "he400b"),
        ("HE 400 B", "he400b"),
        ("hea300", "he300a"),
        ("IPE_300", "ipe300"),
        ("100*100*5", "100x100x5"),
    ],
)
def test_normalise(text: str, reduced: str) -> None:
    assert normalise(text) == reduced


def test_standards_and_types(library: ProfileLibrary) -> None:
    assert library.standards() == ["ansi inch", "BSI", "DIN"]
    assert library.types("din") == ["hea", "ipe"]
    assert library.types("ANSI INCH") == ["c channel"]


def test_sizes_are_read_once_per_file(library: ProfileLibrary) -> None:
    assert library.sizes("DIN", "IPE") == ["IPE100", "IPE200", "IPE300"]
    assert library.sizes("DIN", "ipe") == ["IPE100", "IPE200", "IPE300"]
    assert len(library.asked) == 1  # type: ignore[attr-defined]


def test_resolve_a_configured_size(library: ProfileLibrary) -> None:
    profile = library.resolve("din", "HEA", "HEB400")

    assert (profile.standard, profile.type, profile.size) == ("DIN", "hea", "HE400B")
    assert profile.configuration == "HE400B"
    assert profile.path.endswith("hea.sldlfp")


def test_resolve_an_old_style_file_per_size(library: ProfileLibrary) -> None:
    profile = library.resolve("ansi inch", "c channel", "3 X 5")

    assert profile.size == "3 x 5" and profile.configuration == ""
    assert profile.path.endswith("3 x 5.sldlfp")
    assert library.sizes("ansi inch", "c channel") == ["3 x 5", "4 x 5.4"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            ("ISO", "ipe", "IPE100"),
            "No weldment profile standard 'ISO'; the library has",
        ),
        (("DIN", "ipn", "IPN100"), "No profile type 'ipn' in DIN; it has hea, ipe."),
        (("DIN", "ipe", "IPE330"), "the closest IPE300"),
    ],
)
def test_resolve_explains_what_is_missing(
    library: ProfileLibrary, args: tuple[str, str, str], message: str
) -> None:
    with pytest.raises(LookupError, match=message):
        library.resolve(*args)


def test_search_finds_sizes_by_every_word(library: ProfileLibrary) -> None:
    by_size = library.search("250x250x12")
    by_beam = library.search("HEB400")
    by_words = library.search("ipe 300", standard="DIN")

    assert [p.size for p in by_size] == ["RHS250x250x12.5"]
    assert [(p.standard, p.size) for p in by_beam] == [("DIN", "HE400B")]
    assert [p.size for p in by_words] == ["IPE300"]
    assert len(library.search("", limit=2)) == 2


def test_an_empty_library_has_no_standards(tmp_path: Path) -> None:
    library = ProfileLibrary([str(tmp_path / "nowhere")], lambda _p: [])

    assert library.roots == [] and library.standards() == []


def test_chains_keep_a_chord_straight_through_its_nodes() -> None:
    """Chord pieces and web members meeting at nodes: the chord is one chain."""
    ends = [
        ((0, 0, 0), (2, 0, 0)),  # chord
        ((2, 0, 0), (4, 0, 0)),  # chord
        ((0, 0, 0), (1, 1, 0)),  # web
        ((1, 1, 0), (2, 0, 0)),  # web
    ]

    groups = chains(ends)

    # The chord stays straight; the web member that would bring the chain
    # back to the chord's middle node starts a chain of its own.
    assert groups == [[2, 0, 1], [3]]
    assert all(_no_branch(ends, g) for g in groups)


def _no_branch(ends: list, group: list[int]) -> bool:
    """No point is the end of more than two of the group's segments."""
    counts: dict[tuple, int] = {}
    for i in group:
        for point in ends[i]:
            counts[point] = counts.get(point, 0) + 1
    return max(counts.values()) <= 2


def test_a_closed_frame_is_one_chain() -> None:
    frame = [
        ((0, 0, 0), (2, 0, 0)),
        ((2, 0, 0), (2, 1, 0)),
        ((2, 1, 0), (0, 1, 0)),
        ((0, 1, 0), (0, 0, 0)),
    ]

    assert chains(frame) == [[0, 1, 2, 3]]


def test_a_zig_zag_web_is_one_chain_and_parallel_posts_are_one_each() -> None:
    web = [((0, 0, 0), (1, 1, 0)), ((1, 1, 0), (2, 0, 0)), ((2, 0, 0), (3, 1, 0))]
    posts = [((0, 0, 0), (0, 3, 0)), ((5, 0, 0), (5, 3, 0))]

    assert chains(web) == [[0, 1, 2]]
    assert chains(posts) == [[0], [1]]


def test_a_chain_grows_backwards_from_its_first_segment() -> None:
    ends = [((1, 0, 0), (2, 0, 0)), ((0, 0, 0), (1, 0, 0))]

    assert chains(ends) == [[1, 0]]


def test_a_truss_never_gets_a_branching_group() -> None:
    """A Warren truss drawn panel by panel: every group is a plain chain."""
    ends = []
    for i in range(6):
        ends.append(((2 * i, 0, 0), (2 * i + 2, 0, 0)))
        ends.append(((2 * i, 0, 0), (2 * i + 1, 1.5, 0)))
        ends.append(((2 * i + 1, 1.5, 0), (2 * i + 2, 0, 0)))
    for i in range(5):
        ends.append(((2 * i + 1, 1.5, 0), (2 * i + 3, 1.5, 0)))

    groups = chains(ends)

    assert sorted(i for g in groups for i in g) == list(range(len(ends)))
    assert all(_no_branch(ends, g) for g in groups)
