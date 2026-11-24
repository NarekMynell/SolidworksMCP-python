"""The weldment profile library on disk: standards, profile types and sizes.

SolidWorks keeps structural-member profiles under Tools > Options > File
Locations > Weldment Profiles (``swFileLocationsWeldmentProfiles``, 29), one
folder per standard. Since SolidWorks 2018 each profile type is one library
part (``.sldlfp``) with a configuration per size: ``DIN\\ipe.sldlfp`` holds
``IPE80`` ... ``IPE750x222``. Older libraries have a folder per type and a
file per size; both layouts are read. Nothing here touches COM: the size
names come from ``sizes_of`` (``ISldWorks::GetConfigurationNames``).

Seen on SolidWorks 2025: 9 standards and 114 types. ``DIN`` has IPE (26
sizes), HE A/AA/B/M (96, named ``HE400B``) and UPN; ``BSI`` has SHS (59,
named ``RHS100x100x5``), RHS and CHS; ``iso`` has 4 sizes per type only.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: The profile files.
PROFILE_SUFFIX = ".sldlfp"

#: European beam names agents write one way and the DIN library another:
#: ``HEB400`` is the library's ``HE400B``.
_HE_BEAM = re.compile(r"^he([abm]{1,2})(\d+)$")


@dataclass(frozen=True)
class Profile:
    """One size of one profile type.

    Attributes:
        standard: The standard's folder name, e.g. ``DIN``.
        type: The profile type, e.g. ``ipe`` (the file's or folder's name).
        size: The size, e.g. ``IPE300`` (a configuration name, or the file's
            name in an old-style library).
        path: The ``.sldlfp`` file.
        configuration: The configuration to insert; empty for an old-style
            file that is one size.
    """

    standard: str
    type: str
    size: str
    path: str
    configuration: str

    def as_dict(self) -> dict[str, str]:
        """The reply fields."""
        return {
            "standard": self.standard,
            "type": self.type,
            "size": self.size,
            "path": self.path,
        }


def normalise(text: str) -> str:
    """A name reduced for matching: lower case, ``x`` for ``×``/``*``, no spaces.

    ``"SHS 250 × 250 × 12.5"`` and ``"shs250x250x12.5"`` are the same, and
    ``HEB400`` reads as ``he400b`` (the DIN library's ``HE400B``).
    """
    reduced = re.sub(r"[\s_]+", "", text.lower().replace("×", "x").replace("*", "x"))
    match = _HE_BEAM.match(reduced)
    if match:
        reduced = f"he{match.group(2)}{match.group(1)}"
    return reduced


def _same(a: str, b: str) -> bool:
    return normalise(a) == normalise(b)


_EQUAL_ANGLE = re.compile(r"^l(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)$")


def _expansions(word: str) -> list[str]:
    """A normalised query word and what it is shop shorthand for.

    ``l50x5`` is the equal angle ``l50x50x5``: an angle's thickness is a
    small part of its leg, so ``L<leg>x<t>`` with ``t`` under half the leg
    can only mean that. ``l100x50`` (an unequal angle's legs) stays as it is.
    """
    match = _EQUAL_ANGLE.match(word)
    if match and float(match.group(2)) < float(match.group(1)) / 2:
        leg, thickness = match.groups()
        return [f"l{leg}x{leg}x{thickness}", word]
    return [word]


class ProfileLibrary:
    """The profiles under one or more library folders.

    Args:
        roots: The library folders (the File Locations setting may hold
            several, separated by ``;``).
        sizes_of: The configuration names of a profile file.
    """

    def __init__(self, roots: list[str], sizes_of: Callable[[str], list[str]]) -> None:
        self.roots = [r for r in roots if r and os.path.isdir(r)]
        self._sizes_of = sizes_of
        self._sizes: dict[str, list[str]] = {}

    def standards(self) -> list[str]:
        """The standards' folder names, sorted."""
        names: set[str] = set()
        for root in self.roots:
            for entry in os.scandir(root):
                if entry.is_dir() and self._types_in(entry.path):
                    names.add(entry.name)
        return sorted(names, key=str.lower)

    def _standard_dirs(self, standard: str) -> list[str]:
        """Every folder for ``standard`` (case and spacing don't matter)."""
        found = []
        for root in self.roots:
            for entry in os.scandir(root):
                if entry.is_dir() and _same(entry.name, standard):
                    found.append(entry.path)
        return found

    @staticmethod
    def _types_in(folder: str) -> dict[str, tuple[str, bool]]:
        """A standard folder's types: name -> (file or folder, old style)."""
        types: dict[str, tuple[str, bool]] = {}
        for entry in os.scandir(folder):
            stem, suffix = os.path.splitext(entry.name)
            if entry.is_file() and suffix.lower() == PROFILE_SUFFIX:
                types[stem] = (entry.path, False)
            elif entry.is_dir() and any(
                name.lower().endswith(PROFILE_SUFFIX) for name in os.listdir(entry.path)
            ):
                types[entry.name] = (entry.path, True)
        return types

    def types(self, standard: str) -> list[str]:
        """A standard's profile types, sorted.

        Raises:
            LookupError: No such standard (the message lists them).
        """
        return sorted(self._types(standard), key=str.lower)

    def _types(self, standard: str) -> dict[str, tuple[str, bool]]:
        folders = self._standard_dirs(standard)
        if not folders:
            raise LookupError(
                f"No weldment profile standard {standard!r}; the library has "
                f"{', '.join(self.standards()) or 'none'} (under "
                f"{', '.join(self.roots) or 'no profile folder'})."
            )
        types: dict[str, tuple[str, bool]] = {}
        for folder in folders:
            types.update(self._types_in(folder))
        return types

    def _entry(self, standard: str, profile_type: str) -> tuple[str, str, bool]:
        """(type name, file or folder, old style) for a standard's type.

        Raises:
            LookupError: No such type (the message lists the standard's).
        """
        types = self._types(standard)
        for name, (path, old_style) in types.items():
            if _same(name, profile_type):
                return name, path, old_style
        close = difflib.get_close_matches(profile_type.lower(), list(types), n=3)
        raise LookupError(
            f"No profile type {profile_type!r} in {standard}; it has "
            f"{', '.join(sorted(types, key=str.lower))}."
            + (f" Closest: {', '.join(close)}." if close else "")
        )

    def sizes(self, standard: str, profile_type: str) -> list[str]:
        """A type's sizes, in the library's order."""
        _, path, old_style = self._entry(standard, profile_type)
        if old_style:
            return sorted(
                os.path.splitext(n)[0]
                for n in os.listdir(path)
                if n.lower().endswith(PROFILE_SUFFIX)
            )
        if path not in self._sizes:
            self._sizes[path] = [str(n) for n in self._sizes_of(path) or []]
        return self._sizes[path]

    def resolve(self, standard: str, profile_type: str, size: str) -> Profile:
        """The profile for a standard, type and size.

        Raises:
            LookupError: The standard, type or size isn't in the library; the
                message names the closest sizes.
        """
        name, path, old_style = self._entry(standard, profile_type)
        sizes = self.sizes(standard, profile_type)
        for candidate in sizes:
            if _same(candidate, size):
                if old_style:
                    file = os.path.join(path, candidate + PROFILE_SUFFIX)
                    return Profile(
                        self._standard_name(standard), name, candidate, file, ""
                    )
                return Profile(
                    self._standard_name(standard), name, candidate, path, candidate
                )
        close = difflib.get_close_matches(
            normalise(size), [normalise(s) for s in sizes], n=5, cutoff=0.5
        )
        by_name = {normalise(s): s for s in sizes}
        shown = [by_name[c] for c in close]
        raise LookupError(
            f"No size {size!r} for {standard} {name}; it has {len(sizes)} sizes"
            + (f", the closest {', '.join(shown)}" if shown else "")
            + ". list_weldment_profiles lists them all."
        )

    def _standard_name(self, standard: str) -> str:
        folders = self._standard_dirs(standard)
        return os.path.basename(folders[0]) if folders else standard

    def search(
        self, query: str, standard: str | None = None, limit: int = 50
    ) -> list[Profile]:
        """Sizes whose type and size names contain every word of ``query``.

        ``"250x250x12"`` finds ``RHS250x250x12.5`` in BSI's SHS; ``"HEB400"``
        finds DIN's ``HE400B``; ``"ipe 300"`` finds ``IPE300``; ``"L50x5"``,
        shop shorthand for an equal angle, finds ``L50x50x5``. A size the
        query names exactly comes first, so ``L50x5`` lists ``L50x50x5``
        before ``L50x50x4`` and ``L50x50x6``.

        Args:
            query: Words to look for, e.g. ``"shs 100x100"``.
            standard: Only this standard.
            limit: At most this many results.

        Returns:
            list[Profile]: The matches: exact sizes first, then by standard,
            type and size.
        """
        words = [
            _expansions(normalise(w)) for w in re.split(r"[\s,;]+", query) if w.strip()
        ]
        standards = [standard] if standard else self.standards()
        found: list[tuple[bool, Profile]] = []
        for std in standards:
            for type_name in self.types(std):
                for size in self.sizes(std, type_name):
                    text = normalise(type_name) + "|" + normalise(size)
                    if all(any(a in text for a in word) for word in words):
                        exact = any(normalise(size) in word for word in words)
                        found.append((not exact, self.resolve(std, type_name, size)))
        found.sort(key=lambda match: match[0])
        return [profile for _, profile in found[:limit]]


#: A sketch point, in any consistent unit.
Point = tuple[float, float, float]


def chains(ends: list[tuple[Point, Point]], tolerance: float = 1e-6) -> list[list[int]]:
    """Split segments into groups SolidWorks accepts: chains joined end to end.

    A structural-member group is segments joined end to end without
    branching. SolidWorks refuses a group in which three segments meet at a
    point (seen live: a truss's chords and web in one group came back
    ``None``), so each chain grows from a segment along the straightest
    unused segment at each end. A chord stays one chain through the nodes
    where web members meet it, and a closed frame is one chain, mitred at
    every corner.

    Args:
        ends: Each segment's two end points.
        tolerance: How close two ends must be to be one point.

    Returns:
        list[list[int]]: Groups of segment indexes, each in chain order.
    """

    def key(point: Point) -> tuple[int, int, int]:
        return (
            round(point[0] / tolerance),
            round(point[1] / tolerance),
            round(point[2] / tolerance),
        )

    at: dict[tuple[int, int, int], list[int]] = {}
    for index, (start, end) in enumerate(ends):
        for point in (start, end):
            at.setdefault(key(point), []).append(index)
    unused = set(range(len(ends)))

    def other_end(index: int, point: Point) -> Point:
        start, end = ends[index]
        return end if key(start) == key(point) else start

    def direction(a: Point, b: Point) -> tuple[float, float, float]:
        d = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        length = (d[0] ** 2 + d[1] ** 2 + d[2] ** 2) ** 0.5 or 1.0
        return d[0] / length, d[1] / length, d[2] / length

    def grow(
        node: Point, came_from: Point, degree: dict[Any, int], opposite: Any
    ) -> list[int]:
        """Extend a chain from its open end ``node``; no point gets a third end.

        A segment may end at a point already in the chain only to close the
        loop at the chain's other end (``opposite``), as a closed frame does.
        """
        grown: list[int] = []
        while degree.get(key(node), 0) < 2:
            heading = direction(came_from, node)

            def fits(index: int, at_node: Point = node) -> bool:
                far = key(other_end(index, at_node))
                return degree.get(far, 0) == 0 or (
                    degree.get(far) == 1 and far == opposite
                )

            options = [i for i in at.get(key(node), []) if i in unused and fits(i)]
            if not options:
                return grown
            best = max(
                options,
                key=lambda i: sum(
                    h * d
                    for h, d in zip(
                        heading, direction(node, other_end(i, node)), strict=True
                    )
                ),
            )
            unused.discard(best)
            grown.append(best)
            far_point = other_end(best, node)
            degree[key(node)] = degree.get(key(node), 0) + 1
            degree[key(far_point)] = degree.get(key(far_point), 0) + 1
            came_from, node = node, far_point
        return grown

    groups: list[list[int]] = []
    for first in range(len(ends)):
        if first not in unused:
            continue
        unused.discard(first)
        start, end = ends[first]
        degree = {key(start): 1, key(end): 1}
        forward = grow(end, start, degree, key(start))
        tail = end
        for index in forward:
            tail = other_end(index, tail)
        backward = grow(start, end, degree, key(tail))
        groups.append([*reversed(backward), first, *forward])
    return groups
