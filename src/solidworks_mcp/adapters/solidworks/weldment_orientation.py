"""Which way a structural member's profile faces, and the turn that points its height ``up``.

Measured live on SolidWorks 2025 SP4 (RHS 150x100 and UPN 200 on lines in
eight directions and in four chains) before this was written:

- A member alone in its group (a line of a 3D sketch, drawn start to end
  along ``a``) gets its profile turned by the smallest rotation that takes
  the model's +z to ``a``: the profile sketch's x and y become that rotation
  of the model's x and y. A member along -z is turned half a turn about x.
  So a beam along x or z stands upright, but a rafter sloping in the xy
  plane comes out rolled by its slope (17.7 degrees on a 17.7 degree roof).
- In a chain (one group of segments joined end to end) the first segment
  gets that frame and each next one the previous frame carried round the
  bend by the smallest rotation between the two directions; a chain in one
  plane keeps one axis square to that plane all the way round.
- ``IStructuralMemberGroup.Angle`` turns the profile about ``a`` by the
  right-hand rule, after the frame above.
- ``AlignAxis`` with an ``IMathVector`` as ``AlignmentVector`` changed
  nothing, so the turn is computed here and given as ``Angle``.

Nothing here touches COM.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

Vector = tuple[float, float, float]
Point = tuple[float, float, float]

#: A member this close to the up direction (cosine) has no "up" of its own.
PARALLEL_COS = 0.995
#: Turns closer than this (degrees) are the same turn.
SAME_TURN_DEG = 0.5
#: Model y: the default up, which stands beams, rafters and braces upright.
MODEL_UP: Vector = (0.0, 1.0, 0.0)


def unit(v: Sequence[float]) -> Vector:
    """``v`` scaled to length 1 (``ValueError`` for a zero vector)."""
    length = math.sqrt(sum(c * c for c in v))
    if length < 1e-12:
        raise ValueError("a direction of zero length")
    return (v[0] / length, v[1] / length, v[2] / length)


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    """``a . b``."""
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Sequence[float], b: Sequence[float]) -> Vector:
    """``a x b``."""
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def rotate(v: Sequence[float], axis: Sequence[float], angle: float) -> Vector:
    """``v`` turned by ``angle`` (radians) about the unit ``axis`` (Rodrigues)."""
    c, s = math.cos(angle), math.sin(angle)
    k = cross(axis, v)
    d = dot(axis, v) * (1.0 - c)
    return (
        v[0] * c + k[0] * s + axis[0] * d,
        v[1] * c + k[1] * s + axis[1] * d,
        v[2] * c + k[2] * s + axis[2] * d,
    )


def carry(v: Vector, frm: Vector, to: Vector) -> Vector:
    """``v`` turned by the smallest rotation taking direction ``frm`` to ``to``.

    Opposite directions turn half a turn about the model's x (as SolidWorks
    does for a member along -z) or, when ``frm`` runs along x, about y.
    """
    c = max(-1.0, min(1.0, dot(frm, to)))
    if c > 1.0 - 1e-12:
        return v
    if c < -1.0 + 1e-12:
        pivot = (1.0, 0.0, 0.0) if abs(frm[0]) < 0.9 else (0.0, 1.0, 0.0)
        axis = unit(cross(cross(frm, pivot), frm))
        return rotate(v, axis, math.pi)
    return rotate(v, unit(cross(frm, to)), math.acos(c))


def lone_frame(a: Vector) -> tuple[Vector, Vector]:
    """The profile's x and y for a member alone in its group, along ``a``."""
    z = (0.0, 0.0, 1.0)
    return carry((1.0, 0.0, 0.0), z, a), carry((0.0, 1.0, 0.0), z, a)


def chain_frames(directions: list[Vector]) -> list[tuple[Vector, Vector]]:
    """Each segment's profile x and y in a chain, directions head to tail."""
    x, y = lone_frame(directions[0])
    frames = [(x, y)]
    for frm, to in zip(directions, directions[1:], strict=False):
        x, y = carry(x, frm, to), carry(y, frm, to)
        frames.append((x, y))
    return frames


def turn_to(y: Vector, a: Vector, up: Vector) -> tuple[float, float] | None:
    """The turn about ``a`` (radians, right hand) taking ``y`` toward ``up``.

    Returns:
        ``(turn, weight)``, weight being how square ``up`` is to ``a``
        (0-1); ``None`` when ``up`` runs along the member.
    """
    along = dot(up, a)
    square = (up[0] - along * a[0], up[1] - along * a[1], up[2] - along * a[2])
    weight = math.sqrt(dot(square, square))
    if weight < math.sqrt(1.0 - PARALLEL_COS**2):
        return None
    target = unit(square)
    return math.atan2(dot(cross(y, target), a), dot(y, target)), weight


def _apart(a: float, b: float, period: float = 360.0) -> float:
    """How far apart two turns are, degrees, for a profile that repeats every ``period``."""
    d = math.degrees(a - b) % period
    return min(d, period - d)


#: Words naming profiles that aren't the same turned half a turn: angles,
#: channels, tees (library types such as 'angle iron', 'upn', 'c channel',
#: 'ea as-nzs', 'l angle').
_ASYMMETRIC_WORDS = {
    "angle",
    "channel",
    "upn",
    "upe",
    "tee",
    "ea",
    "ua",
    "l",
    "c",
    "u",
    "t",
}


def symmetric(profile_type: str, size: str) -> bool:
    """Whether a profile looks the same turned half a turn (tubes, I-sections, bars)."""
    words = set(re.split(r"[^a-z]+", f"{profile_type} {size}".lower()))
    return not words & _ASYMMETRIC_WORDS


def group_turn(
    directions: list[Vector], up: Vector, half_turn_same: bool = False
) -> tuple[float | None, float]:
    """The ``Angle`` that points a group's profile height at ``up``.

    A chain has one ``Angle``: it is taken from the segment most square to
    ``up``, and ``spread`` says how far the others then miss ``up``
    (0 for a chain in one plane with ``up`` in that plane). A closed loop's
    far side comes round upside down: no miss for a profile that looks the
    same turned half a turn (``half_turn_same``).

    Returns:
        ``(angle, spread)``: radians (``None`` when ``up`` runs along every
        segment) and degrees.
    """
    frames = chain_frames(directions)
    turns = [
        found
        for a, (_x, y) in zip(directions, frames, strict=True)
        if (found := turn_to(y, a, up)) is not None
    ]
    if not turns:
        return None, 0.0
    best = max(turns, key=lambda t: t[1])[0]
    period = 180.0 if half_turn_same else 360.0
    return best, max(_apart(t[0], best, period) for t in turns)


def head_to_tail(
    ends: list[tuple[Point, Point]], chain: list[int], tolerance: float = 1e-6
) -> list[tuple[Point, Point]]:
    """A chain's segments turned to run head to tail, in chain order.

    The first segment points at the second; a lone segment keeps its way.
    """
    if len(chain) == 1:
        return [ends[chain[0]]]

    def same(p: Point, q: Point) -> bool:
        return math.dist(p, q) <= tolerance

    first_start, first_end = ends[chain[0]]
    second = ends[chain[1]]
    if same(first_start, second[0]) or same(first_start, second[1]):
        first_start, first_end = first_end, first_start
    ran = [(first_start, first_end)]
    for index in chain[1:]:
        start, end = ends[index]
        if not same(start, ran[-1][1]):
            start, end = end, start
        ran.append((start, end))
    return ran


def direction(start: Point, end: Point) -> Vector:
    """The unit direction from ``start`` to ``end``."""
    return unit((end[0] - start[0], end[1] - start[1], end[2] - start[2]))


def is_vertical(a: Vector, up: Vector = MODEL_UP) -> bool:
    """Whether a member runs along ``up`` (a column, for the default up)."""
    return abs(dot(a, up)) >= PARALLEL_COS


def roll_error(
    wanted_y: Vector, axis: Vector, face_normals: list[Vector]
) -> float | None:
    """Degrees between the wanted height direction and the nearest flat side.

    A profile with flat sides (RHS, I, U, angles, flats) has side faces
    whose normals run along its x or its y, so when its height points at
    ``wanted_y`` some side's normal is parallel or square to it; a profile
    rolled by r degrees shows r (or 90 - r) instead. ``None`` when the body
    has no flat side along its axis (round tubes and bars). A quarter turn
    can't be told from none.
    """
    sides = [n for n in face_normals if abs(dot(n, axis)) < 0.02]
    if not sides:
        return None
    best = 90.0
    for n in sides:
        tilt = math.degrees(math.acos(min(1.0, abs(dot(unit(n), wanted_y)))))
        best = min(best, tilt, 90.0 - tilt)
    return best


def chained(
    segments: list[list[float]], groups: list[list[int]]
) -> tuple[list[list[float]], list[list[int]]]:
    """Segments reordered chain by chain, each chain head to tail.

    Returns:
        The segments in the new order and the groups as runs of the new
        indexes, so a chain is drawn the way it is traversed.
    """
    ends = [((s[0], s[1], s[2]), (s[3], s[4], s[5])) for s in segments]
    ordered: list[list[float]] = []
    plan: list[list[int]] = []
    for chain in groups:
        run = head_to_tail(ends, chain)
        plan.append(list(range(len(ordered), len(ordered) + len(run))))
        ordered.extend([*start, *end] for start, end in run)
    return ordered, plan
