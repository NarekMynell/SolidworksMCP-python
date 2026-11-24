"""A weldment's joints, measured: bodies that run into each other, member ends left open, bodies touching nothing.

Checked live on SolidWorks 2025 SP4 before it was written, on a 312-member
lattice figure drawn node to node and on trimmed frames:

- ``IModeler::CheckInterferenceBetweenTwoBodies(b1, b2, False, faces1,
  faces2, bodies)`` needs byref ``VARIANT`` out-parameters (a plain call is
  "Parameter not optional"); it returns the intersection as temporary
  bodies, whose volume (``IBody2::GetMassProperties``) is the overlap. It
  costs about 0.1 s a pair (1463 pairs: 165 s), so a pair whose bounding
  boxes share no volume is never asked.
- Bodies that only touch (a brace trimmed to a chord's face) don't
  interfere, coincident option or not: the call returns ``False`` with no
  faces. So contact is measured instead: points on a planar face
  (``IFace2::GetTessTriangles``), moved a hair along its outward
  ``IFace2::Normal``, and ``IFace2::GetClosestPointOn`` on the nearby faces
  of nearby bodies.
- ``IFeatureManager::InsertEndCapFeature3`` caps the selected end faces. Its
  last argument places the cap: 0 outside the end (the member keeps its
  length, the cap adds its thickness), 1 flush (the member is shortened by
  the thickness), 2 inside the tube at ``DepthOffset`` from the end, sized
  to the inside. ``OffsetValue`` (with ``BIsGivenOffset``) insets the cap
  from the profile's outside. ``IEntity::Select4`` needs a ``VT_DISPATCH``
  null for its select data.
- Every call into SolidWorks costs about 3 ms, so a face is read only as far
  as needed: ``IFace2::Normal`` is a zero vector on a face that isn't planar
  (no ``GetSurface`` and ``IsPlane`` needed), its area only when planar, its
  points only when it is probed.
- A member's length and width come from ``IBody2::GetExtremePoint`` (byref
  double out-parameters) along its axis and square to it: bounding boxes of
  a diagonal brace's faces span the whole diagonal, and called it wide.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .. import sw_type_info as _sw_type_info
from .com_utils import as_list

#: ``swBodyType_e.swSolidBody``.
SOLID_BODY = 0
#: ``swBodyOperationType_e`` is not needed: the modeler's interference call
#: returns the intersection itself.
#: How far a probe point sits outside the face it samples, m.
PROBE_M = 0.0002
#: A planar face whose normal is within this cosine of a member's axis is an
#: end face (cuts up to about 70 degrees off square).
END_COSINE = 0.34
#: A body this many times longer than its widest cross-section is a member.
MEMBER_SLENDERNESS = 2.5
#: Points sampled on each face, spread over it.
SAMPLES = 12
#: An end with at least this share of its points touching something is
#: closed. A tube landing on one as wide as itself sits on that tube's corner
#: radii: two of its walls touch nothing (a 1-2 mm gap, welded with a flare
#: bevel) and the other two only between the radii, about a third of its
#: points (seen live on 40x40 tubes); an end in mid-air touches with none.
CLOSED_SHARE = 0.25
#: ``InsertEndCapFeature3``'s placement argument.
CAP_PLACEMENT = {"outward": 0, "inward": 1, "internal": 2}

Vector = tuple[float, float, float]
Box = tuple[float, float, float, float, float, float]


# --- geometry (pure) ---


def sub(a: Iterable[float], b: Iterable[float]) -> Vector:
    """``a - b``."""
    x, y, z = a
    p, q, r = b
    return (x - p, y - q, z - r)


def dot(a: Iterable[float], b: Iterable[float]) -> float:
    """The dot product."""
    return sum(i * j for i, j in zip(a, b, strict=True))


def unit(a: Iterable[float]) -> Vector:
    """``a`` scaled to length 1 (a zero vector stays zero)."""
    x, y, z = a
    length = math.sqrt(x * x + y * y + z * z)
    return (0.0, 0.0, 0.0) if length == 0 else (x / length, y / length, z / length)


def boxes_near(a: Box, b: Box, tol: float) -> bool:
    """Whether two boxes overlap or come within ``tol`` of each other."""
    return all(a[k] <= b[k + 3] + tol and b[k] <= a[k + 3] + tol for k in range(3))


def box_overlap_volume(a: Box, b: Box) -> float:
    """The volume two boxes share (0 when they only touch or are apart)."""
    volume = 1.0
    for k in range(3):
        side = min(a[k + 3], b[k + 3]) - max(a[k], b[k])
        if side <= 0:
            return 0.0
        volume *= side
    return volume


def in_box(point: Iterable[float], box: Box, tol: float) -> bool:
    """Whether a point lies in a box grown by ``tol``."""
    return all(box[k] - tol <= c <= box[k + 3] + tol for k, c in enumerate(point))


def smallest_eigenvector(m: list[list[float]]) -> Vector:
    """The eigenvector of a symmetric 3x3 matrix with the smallest eigenvalue (Jacobi rotations)."""
    a = [row[:] for row in m]
    v = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
    for _ in range(50):
        p, q = max(((0, 1), (0, 2), (1, 2)), key=lambda ij: abs(a[ij[0]][ij[1]]))
        if abs(a[p][q]) < 1e-15:
            break
        theta = 0.5 * math.atan2(2 * a[p][q], a[q][q] - a[p][p])
        c, s = math.cos(theta), math.sin(theta)
        for k in range(3):
            akp, akq = a[k][p], a[k][q]
            a[k][p], a[k][q] = c * akp - s * akq, s * akp + c * akq
        for k in range(3):
            apk, aqk = a[p][k], a[q][k]
            a[p][k], a[q][k] = c * apk - s * aqk, s * apk + c * aqk
        for k in range(3):
            vkp, vkq = v[k][p], v[k][q]
            v[k][p], v[k][q] = c * vkp - s * vkq, s * vkp + c * vkq
    smallest = min(range(3), key=lambda i: a[i][i])
    return unit((v[0][smallest], v[1][smallest], v[2][smallest]))


def member_axis(faces: list[tuple[Vector, float]]) -> Vector | None:
    """A straight member's axis from its planar faces: the direction their normals avoid.

    The side faces of a prism have normals square to its axis and carry most
    of the area, so the axis is the least-weighted direction of
    ``sum(area * n n^T)``. A round tube has no planar sides: its planar
    faces are its ends, whose normal is the axis.

    Args:
        faces: Each planar face's outward unit normal and area.

    Returns:
        Vector | None: The axis (sign arbitrary), or ``None`` with no planar face.
    """
    if not faces:
        return None
    m = [[sum(a * n[i] * n[j] for n, a in faces) for j in range(3)] for i in range(3)]
    largest = max(faces, key=lambda f: f[1])[0]
    if all(abs(dot(n, largest)) > 0.95 for n, _ in faces):
        return unit(largest)  # one direction only: a round tube's ends
    return smallest_eigenvector(m)


def triangle_samples(tess: list[float], count: int = SAMPLES) -> list[Vector]:
    """Points spread over a face: centroids of its tessellation triangles (9 numbers each).

    The largest triangle's centroid first, then each time the centroid
    farthest from those already taken, so every wall of a tube's ring-shaped
    end gets its share.
    """
    centroids: list[tuple[float, Vector]] = []
    for i in range(0, len(tess) - 8, 9):
        a, b, c = tess[i : i + 3], tess[i + 3 : i + 6], tess[i + 6 : i + 9]
        ab, ac = sub(b, a), sub(c, a)
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        area = 0.5 * math.sqrt(dot(cross, cross))
        if area > 0:
            centroid = (
                (a[0] + b[0] + c[0]) / 3.0,
                (a[1] + b[1] + c[1]) / 3.0,
                (a[2] + b[2] + c[2]) / 3.0,
            )
            centroids.append((area, centroid))
    if not centroids:
        return []
    chosen = [max(centroids, key=lambda t: t[0])[1]]
    rest = [p for _, p in centroids if p != chosen[0]]
    while rest and len(chosen) < count:
        far = max(rest, key=lambda p: min(math.dist(p, q) for q in chosen))
        chosen.append(far)
        rest.remove(far)
    return chosen


def groups_of(names: list[str], links: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Connected groups of bodies (union-find), largest first."""
    parent = {n: n for n in names}

    def root(n: str) -> str:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b in links:
        if a in parent and b in parent:
            parent[root(a)] = root(b)
    found: dict[str, list[str]] = {}
    for n in names:
        found.setdefault(root(n), []).append(n)
    return sorted(found.values(), key=len, reverse=True)


def mm(point: Iterable[float]) -> list[float]:
    """Metres to millimetres, rounded to 0.1 mm."""
    return [round(c * 1000.0, 1) for c in point]


# --- what SolidWorks tells about a body ---


@dataclass
class Face:
    """A face of a body: planar ones carry their normal and sample points."""

    ref: Any
    box: Box
    planar: bool
    normal: Vector = (0.0, 0.0, 0.0)
    area: float = 0.0
    samples: list[Vector] | None = None


@dataclass
class Body:
    """A solid body, its box, and (read when first needed) its faces and member ends."""

    name: str
    ref: Any
    box: Box
    faces: list[Face] | None = None
    axis: Vector | None = None
    member: bool = False
    ends: list[Face] = field(default_factory=list)
    make_double: Callable[[], Any] | None = None


def read_bodies(
    adapter: Any, part: Any, make_double: Callable[[], Any] | None = None
) -> list[Body]:
    """The part's visible solid bodies with their bounding boxes (m).

    ``make_double`` makes a byref double out-parameter, for measuring a
    member's length and width; without it the faces' boxes are used, which
    is right only for members along the model's axes.
    """
    found = []
    for raw in as_list(
        adapter._attempt(lambda: part.GetBodies2(SOLID_BODY, True), default=None)
    ):
        body = _sw_type_info.flagged(raw, "IBody2")
        box = as_list(adapter._attempt(lambda b=body: b.GetBodyBox(), default=None))
        if len(box) == 6:
            found.append(
                Body(
                    str(body.Name),
                    body,
                    tuple(float(v) for v in box),  # type: ignore[arg-type]
                    make_double=make_double,
                )
            )
    return found


def read_faces(adapter: Any, body: Body) -> list[Face]:
    """A body's faces (cached), and whether it is a member and which faces are its ends."""
    if body.faces is not None:
        return body.faces
    faces = []
    for raw in as_list(adapter._attempt(lambda: body.ref.GetFaces(), default=None)):
        face = _sw_type_info.flagged(raw, "IFace2")
        box = as_list(adapter._attempt(lambda f=face: f.GetBox(), default=None))
        if len(box) != 6:
            continue
        normal = [
            float(v)
            for v in as_list(adapter._attempt(lambda f=face: f.Normal, default=None))
        ]
        planar = len(normal) == 3 and any(abs(c) > 1e-9 for c in normal)
        entry = Face(face, tuple(float(v) for v in box), planar)  # type: ignore[arg-type]
        if planar:
            entry.normal = unit(normal)
            entry.area = float(
                adapter._attempt(lambda f=face: f.GetArea(), default=0.0) or 0.0
            )
        faces.append(entry)
    body.faces = faces
    planars = [f for f in faces if f.planar]
    body.axis = member_axis([(f.normal, f.area) for f in planars])
    if body.axis is not None:
        spans = _span(adapter, body, body.axis)
        widest = max(_span(adapter, body, d) for d in _square_to(body.axis))
        if widest > 0 and spans >= MEMBER_SLENDERNESS * widest:
            body.member = True
            body.ends = [
                f for f in planars if abs(dot(f.normal, body.axis)) >= END_COSINE
            ]
    return faces


def samples_of(adapter: Any, face: Face) -> list[Vector]:
    """Points spread over a planar face (read when first needed)."""
    if face.samples is None:
        tess = [
            float(v)
            for v in as_list(
                adapter._attempt(lambda: face.ref.GetTessTriangles(True), default=None)
            )
        ]
        face.samples = triangle_samples(tess)
    return face.samples


def _span(adapter: Any, body: Body, direction: Vector) -> float:
    """How far a body reaches along a direction: its extreme points both ways."""
    if body.make_double is not None:
        ends = []
        for sign in (1.0, -1.0):
            outs = [body.make_double() for _ in range(3)]
            ok = adapter._attempt(
                lambda s=sign, o=outs: body.ref.GetExtremePoint(
                    s * direction[0], s * direction[1], s * direction[2], *o
                ),
                default=False,
            )
            if not ok:
                break
            ends.append([float(getattr(o, "value", o)) for o in outs])
        if len(ends) == 2:
            return abs(dot(sub(ends[0], ends[1]), direction))
    return _extent(body, direction)


def _extent(body: Body, direction: Vector) -> float:
    """How far a body's faces' box corners spread along a direction (axis-aligned members)."""
    points = [corner for f in body.faces or [] for corner in (f.box[:3], f.box[3:])]
    if not points:
        return 0.0
    values = [dot(p, direction) for p in points]
    return max(values) - min(values)


def _square_to(axis: Vector) -> tuple[Vector, Vector]:
    """Two directions square to ``axis`` and to each other."""
    helper = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = unit(
        (
            axis[1] * helper[2] - axis[2] * helper[1],
            axis[2] * helper[0] - axis[0] * helper[2],
            axis[0] * helper[1] - axis[1] * helper[0],
        )
    )
    v = (
        axis[1] * u[2] - axis[2] * u[1],
        axis[2] * u[0] - axis[0] * u[2],
        axis[0] * u[1] - axis[1] * u[0],
    )
    return u, v


# --- overlaps ---


def overlaps(
    adapter: Any,
    modeler: Any,
    bodies: list[Body],
    least_m3: float,
    make_out: Callable[[], Any],
    only: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Every pair of bodies sharing more than ``least_m3`` of volume.

    Args:
        adapter: The adapter (for ``_attempt``).
        modeler: ``IModeler``.
        bodies: The part's bodies.
        least_m3: Overlaps this small or smaller count as touching.
        make_out: Makes one byref ``VARIANT`` out-parameter.
        only: When given, only pairs with at least one of these bodies.

    Returns:
        tuple: The overlapping pairs (``bodies``, ``volume_mm3``, ``at_mm``),
        largest first, and how many pairs were asked.
    """
    found: list[dict[str, Any]] = []
    asked = 0
    for i, a in enumerate(bodies):
        for b in bodies[i + 1 :]:
            if box_overlap_volume(a.box, b.box) <= least_m3:
                continue
            if only is not None and a.name not in only and b.name not in only:
                continue
            asked += 1
            faces_a, faces_b, common = make_out(), make_out(), make_out()
            hit = adapter._attempt(
                lambda a=a, b=b, fa=faces_a, fb=faces_b, c=common: (
                    modeler.CheckInterferenceBetweenTwoBodies(
                        a.ref, b.ref, False, fa, fb, c
                    )
                ),
                default=False,
            )
            if not hit:
                continue
            volume, centre = 0.0, None
            for raw in as_list(getattr(common, "value", common)):
                values = as_list(
                    adapter._attempt(
                        lambda r=raw: _sw_type_info.flagged(
                            r, "IBody2"
                        ).GetMassProperties(1.0),
                        default=None,
                    )
                )
                if len(values) >= 4:
                    volume += float(values[3])
                    centre = centre or tuple(float(v) for v in values[:3])
            if volume > least_m3:
                found.append(
                    {
                        "bodies": [a.name, b.name],
                        "volume_mm3": round(volume * 1e9, 1),
                        "at_mm": mm(centre)
                        if centre
                        else mm(_box_centre(a.box, b.box)),
                    }
                )
    found.sort(key=lambda o: -o["volume_mm3"])
    return found, asked


def _box_centre(a: Box, b: Box) -> Vector:
    """The middle of the box two boxes share."""
    return tuple(  # type: ignore[return-value]
        (max(a[k], b[k]) + min(a[k + 3], b[k + 3])) / 2.0 for k in range(3)
    )


# --- contacts and open ends ---


def _touches(adapter: Any, point: Vector, other: Body, reach: float) -> bool:
    """Whether a point lies within ``reach`` of a body's surface."""
    if not in_box(point, other.box, reach):
        return False
    for face in read_faces(adapter, other):
        if not in_box(point, face.box, reach):
            continue
        closest = as_list(
            adapter._attempt(
                lambda f=face: f.ref.GetClosestPointOn(point[0], point[1], point[2]),
                default=None,
            )
        )
        if (
            len(closest) >= 3
            and math.dist(point, [float(c) for c in closest[:3]]) <= reach
        ):
            return True
    return False


def contacts(
    adapter: Any, bodies: list[Body], reach: float, only: set[str] | None = None
) -> tuple[set[tuple[str, str]], list[dict[str, Any]]]:
    """Which bodies touch, and which member ends touch nothing.

    Every member's end faces are probed, and every planar face of a body
    that isn't a member (a plate, a cap), each at its sample points moved
    ``PROBE_M`` outward. A face touches a body when a probe point lies
    within ``reach`` of it.

    Returns:
        tuple: Touching pairs, and the ends less than ``CLOSED_SHARE``
        covered (``body``, ``at_mm``, ``normal``, ``covered_percent``: 0
        for an open end, more for an end that hangs off what it meets).
    """
    links: set[tuple[str, str]] = set()
    open_ends = []
    for body in bodies:
        if only is not None and body.name not in only:
            continue
        read_faces(adapter, body)
        probed = body.ends if body.member else [f for f in body.faces or [] if f.planar]
        near = [
            o for o in bodies if o is not body and boxes_near(body.box, o.box, reach)
        ]
        for face in probed:
            touching = 0
            samples = samples_of(adapter, face)
            for sample in samples:
                point: Vector = (
                    sample[0] + face.normal[0] * PROBE_M,
                    sample[1] + face.normal[1] * PROBE_M,
                    sample[2] + face.normal[2] * PROBE_M,
                )
                hit = next(
                    (o for o in near if _touches(adapter, point, o, reach)), None
                )
                if hit is not None:
                    touching += 1
                    first, second = sorted((body.name, hit.name))
                    links.add((first, second))
            if body.member and samples and touching < CLOSED_SHARE * len(samples):
                centre = tuple(
                    sum(s[k] for s in samples) / len(samples) for k in range(3)
                )
                open_ends.append(
                    {
                        "body": body.name,
                        "at_mm": mm(centre),
                        "normal": [round(c, 3) for c in face.normal],
                        "covered_percent": round(100.0 * touching / len(samples)),
                        "face": face,
                    }
                )
    return links, open_ends


def check(
    adapter: Any,
    part: Any,
    modeler: Any,
    make_out: Callable[[], Any],
    overlap_mm3: float,
    contact_mm: float,
    listed: int,
    only: set[str] | None = None,
    make_double: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """The joint check of a weldment part, for ``check_joints``.

    ``only`` limits it to those bodies: their overlaps with any body, their
    ends, and whether they touch anything; ``None`` checks every body.
    """
    started = time.monotonic()
    bodies = read_bodies(adapter, part, make_double)
    pairs, asked = overlaps(
        adapter, modeler, bodies, overlap_mm3 * 1e-9, make_out, only
    )
    links, open_ends = contacts(adapter, bodies, contact_mm / 1000.0, only)
    for pair in pairs:
        first, second = sorted(pair["bodies"])
        links.add((first, second))
    # An end buried in a body it overlaps is that overlap, not an open end.
    by_name = {b.name: b for b in bodies}
    overlapping: dict[str, list[Body]] = {}
    for pair in pairs:
        first, second = pair["bodies"]
        overlapping.setdefault(first, []).append(by_name[second])
        overlapping.setdefault(second, []).append(by_name[first])
    open_ends = [
        end
        for end in open_ends
        if not any(
            in_box([c / 1000.0 for c in end["at_mm"]], other.box, contact_mm / 1000.0)
            for other in overlapping.get(end["body"], [])
        )
    ]
    names = [b.name for b in bodies]
    if only is not None:
        # Outside the subset only the bodies something in it touches count.
        touched = {n for link in links for n in link}
        names = [n for n in names if n in only or n in touched]
    groups = groups_of(names, links)
    loose = [g[0] for g in groups if len(g) == 1 and (only is None or g[0] in only)]
    if len(bodies) < 2:
        loose = []
    for end in open_ends:
        end.pop("face", None)
    fully_open = [e for e in open_ends if e["covered_percent"] == 0]
    partly = [e for e in open_ends if e["covered_percent"] > 0]
    return {
        "bodies": len(bodies),
        "checked_bodies": len(bodies) if only is None else len(only),
        "members": sum(1 for b in bodies if b.member),
        "overlaps": {
            "count": len(pairs),
            "volume_mm3": round(sum(p["volume_mm3"] for p in pairs), 1),
            "pairs": pairs[:listed],
        },
        "open_ends": {"count": len(fully_open), "ends": fully_open[:listed]},
        "partly_open_ends": {"count": len(partly), "ends": partly[:listed]},
        "loose": loose[:listed],
        "loose_count": len(loose),
        "groups": len(groups),
        "group_sizes": [len(g) for g in groups[:listed]],
        "clean": not pairs and not loose and not partly,
        "pairs_checked": asked,
        "seconds": round(time.monotonic() - started, 1),
    }


def end_faces_at(
    adapter: Any, bodies: list[Body], points_mm: list[list[float]], reach_mm: float
) -> tuple[list[Face], list[list[float]]]:
    """The member end face nearest each point, and the points no end face is near."""
    chosen: list[Face] = []
    missed = []
    for point_mm in points_mm:
        point = tuple(c / 1000.0 for c in point_mm)
        best: tuple[float, Face] | None = None
        for body in bodies:
            if not in_box(point, body.box, reach_mm / 1000.0):
                continue
            read_faces(adapter, body)
            for face in body.ends if body.member else []:
                closest = as_list(
                    adapter._attempt(
                        lambda f=face, p=point: f.ref.GetClosestPointOn(
                            p[0], p[1], p[2]
                        ),
                        default=None,
                    )
                )
                if len(closest) < 3:
                    continue
                distance = math.dist(point, [float(c) for c in closest[:3]])
                if distance <= reach_mm / 1000.0 and (
                    best is None or distance < best[0]
                ):
                    best = (distance, face)
        if best is None:
            missed.append(point_mm)
        elif best[1] not in chosen:
            chosen.append(best[1])
    return chosen, missed
