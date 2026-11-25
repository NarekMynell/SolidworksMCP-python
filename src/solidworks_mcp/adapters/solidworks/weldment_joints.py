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
#: An overlap thinner than this (m) is touching, however large: plates laid
#: face to face on a slope overlapped 3.6 mm^3 over 4200 mm^2, under a
#: thousandth of a millimetre deep (seen live). Its depth is twice its volume
#: over its surface area, a thin slab's thickness.
TOUCH_DEPTH_M = 1e-5
#: A side or face lapped on another touches within this (m), whatever
#: ``contact_mm`` an end's weld gap needs: a leaf hung 3 mm off its post is
#: not joined to it.
SIDE_REACH_M = 0.001
#: A coped (curved) end reaches the member's extreme point within this (m)
#: and spans at least this share of its section; a bolt hole near the end
#: does neither (a 2 mm fascia's holes were taken for ends, seen live).
COPED_TIP_M = 0.001
COPED_SPAN = 0.5
#: An end with at least this share of its points touching something is
#: closed. A tube landing on one as wide as itself sits on that tube's corner
#: radii: two of its walls touch nothing (a 1-2 mm gap, welded with a flare
#: bevel) and the other two only between the radii, about a third of its
#: points (seen live on 40x40 tubes); an end in mid-air touches with none.
CLOSED_SHARE = 0.25
#: A hollow end's opening counts as covered by a body this near (m): the
#: weld bridges a gap that small (a tube landing on one as wide sits on its
#: corner radii, 1-2 mm off).
OPENING_REACH_M = 0.003
#: A hollow end whose ring touches at this share or more is closed (bore covered or joined).
FULL_RING = 0.9
#: A hollow end is closed when at least this share of its opening probes touch.
OPENING_SHARE = 0.8
#: How far toward the ring the opening probes sit (share of its extent).
OPENING_SPREAD = 0.4
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
    #: A curved face's outward normal at each sample (a coped end, a tube's side).
    sample_normals: list[Vector] | None = None
    #: The tessellation, 9 numbers a triangle (read when first needed).
    tess: list[float] | None = None
    #: A curved face's vertex normals, 9 numbers a triangle like ``tess``.
    norms: list[float] | None = None


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
            ] + _coped_ends(adapter, body, faces, spans, widest)
    return faces


def _coped_ends(
    adapter: Any, body: Body, faces: list[Face], length: float, width: float
) -> list[Face]:
    """A member's curved end faces: an end coped round a tube touches it on a saddle.

    A curved face is an end when it is short along the member (under half
    its length), reaches one of the member's extreme points (within
    ``COPED_TIP_M``) and spans at least ``COPED_SPAN`` of its section; its
    normal is set along the axis, out of that end. A standoff coped onto a
    round handle touched it only there (seen live).
    """
    axis = body.axis
    assert axis is not None
    across = _square_to(axis)
    ends = []
    for face in faces:
        if face.planar:
            continue
        # The face's box as seen along the axis: on a diagonal member the
        # box's two corners alone measured a tube's side as short (an end).
        spread = sum(abs(axis[k]) * (face.box[k + 3] - face.box[k]) for k in range(3))
        if spread > 0.5 * length:
            continue  # a tube's side, a corner radius
        points = _vertices(adapter, face)
        if not points:
            continue
        span = max(
            max(dot(p, d) for p in points) - min(dot(p, d) for p in points)
            for d in across
        )
        if span < COPED_SPAN * width:
            continue  # a hole, a rounded corner
        for sign in (1.0, -1.0):
            out: Vector = (sign * axis[0], sign * axis[1], sign * axis[2])
            tip = _tip(adapter, body, out)
            if max(dot(p, out) for p in points) >= tip - COPED_TIP_M:
                face.normal = out
                ends.append(face)
                break
    return ends


def _tip(adapter: Any, body: Body, direction: Vector) -> float:
    """How far a body reaches along ``direction``: its extreme point's projection."""
    if body.make_double is not None:
        outs = [body.make_double() for _ in range(3)]
        ok = adapter._attempt(
            lambda: body.ref.GetExtremePoint(*direction, *outs), default=False
        )
        if ok:
            return dot([float(getattr(o, "value", o)) for o in outs], direction)
    corners = [c for f in body.faces or [] for c in (f.box[:3], f.box[3:])]
    return max(dot(c, direction) for c in corners)


def _tessellation(adapter: Any, face: Face) -> list[float]:
    """A face's tessellation triangles, 9 numbers each (cached)."""
    if face.tess is None:
        face.tess = [
            float(v)
            for v in as_list(
                adapter._attempt(lambda: face.ref.GetTessTriangles(True), default=None)
            )
        ]
    return face.tess


def _vertices(adapter: Any, face: Face) -> list[Vector]:
    """The corners of a face's tessellation triangles."""
    tess = _tessellation(adapter, face)
    return [(tess[i], tess[i + 1], tess[i + 2]) for i in range(0, len(tess) - 2, 3)]


def samples_of(adapter: Any, face: Face) -> list[Vector]:
    """Points spread over a face (read when first needed); a curved face's normals too."""
    if face.samples is None:
        tess = _tessellation(adapter, face)
        face.samples = triangle_samples(tess)
        if not face.planar and face.samples:
            face.norms = [
                float(v)
                for v in as_list(
                    adapter._attempt(lambda: face.ref.GetTessNorms(), default=None)
                )
            ]
            face.sample_normals = _normals_at(
                tess, face.norms, face.samples, face.normal
            )
    return face.samples


def _normals_at(
    tess: list[float], norms: list[float], points: list[Vector], fallback: Vector
) -> list[Vector]:
    """Each sample's outward normal: its triangle's averaged vertex normals."""
    if len(norms) != len(tess):
        return [fallback] * len(points)
    found = []
    for point in points:
        best, normal = math.inf, fallback
        for i in range(0, len(tess) - 8, 9):
            centroid = tuple(
                (tess[i + k] + tess[i + 3 + k] + tess[i + 6 + k]) / 3.0
                for k in range(3)
            )
            distance = math.dist(centroid, point)
            if distance < best:
                summed = tuple(
                    norms[i + k] + norms[i + 3 + k] + norms[i + 6 + k] for k in range(3)
                )
                if any(abs(c) > 1e-12 for c in summed):
                    best, normal = distance, unit(summed)
        found.append(normal)
    return found


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
            # Each lump is judged by its own depth: a thin sliver beside a
            # deep chunk must not average the chunk away.
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
                if len(values) < 4:
                    continue
                lump, area = (
                    float(values[3]),
                    (float(values[4]) if len(values) > 4 else 0.0),
                )
                if area and 2 * lump / area < TOUCH_DEPTH_M:
                    continue
                volume += lump
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


def is_hollow(adapter: Any, face: Face) -> bool:
    """Whether a member's end face is a ring (a hollow section's end: two loops or more)."""
    loops = adapter._attempt(lambda: face.ref.GetLoopCount(), default=1)
    return int(loops or 1) > 1


def opening_points(face: Face, samples: list[Vector]) -> list[Vector]:
    """Five points across a hollow end's opening, just outside it: the centre and half way out.

    The opening is centred on the end face's box (a hollow section's end
    is symmetric about its centre, square or mitred); the points sit 0.4 of
    the way to the ring's sampled extent, inside the bore of any wall
    thinner than a fifth of the section.
    """
    n = face.normal
    box = face.box
    c0 = tuple((box[k] + box[k + 3]) / 2.0 for k in range(3))
    s0 = samples[0]
    lift = dot(sub(c0, s0), n)
    c = tuple(c0[k] - lift * n[k] for k in range(3))
    helper = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = unit(
        (
            n[1] * helper[2] - n[2] * helper[1],
            n[2] * helper[0] - n[0] * helper[2],
            n[0] * helper[1] - n[1] * helper[0],
        )
    )
    v = (
        n[1] * u[2] - n[2] * u[1],
        n[2] * u[0] - n[0] * u[2],
        n[0] * u[1] - n[1] * u[0],
    )
    eu = max(abs(dot(sub(p, c), u)) for p in samples)
    ev = max(abs(dot(sub(p, c), v)) for p in samples)
    points = [c]
    for axis, extent in ((u, eu), (v, ev)):
        for sign in (1.0, -1.0):
            points.append(
                tuple(c[k] + sign * OPENING_SPREAD * extent * axis[k] for k in range(3))
            )
    return [
        (p[0] + n[0] * PROBE_M, p[1] + n[1] * PROBE_M, p[2] + n[2] * PROBE_M)
        for p in points
    ]


def _pair(a: str, b: str) -> tuple[str, str]:
    first, second = sorted((a, b))
    return first, second


def _probe(
    adapter: Any, face: Face, near: list[Body], reach: float
) -> tuple[list[Vector], int, set[str]]:
    """A face's sample points, how many touch a nearby body, and the bodies they touch."""
    samples = samples_of(adapter, face)
    normals = face.sample_normals or [face.normal] * len(samples)
    touching = 0
    hits: set[str] = set()
    for sample, out in zip(samples, normals, strict=False):
        point: Vector = (
            sample[0] + out[0] * PROBE_M,
            sample[1] + out[1] * PROBE_M,
            sample[2] + out[2] * PROBE_M,
        )
        hit = next((o for o in near if _touches(adapter, point, o, reach)), None)
        if hit is not None:
            touching += 1
            hits.add(hit.name)
    return samples, touching, hits


def _face_touches(adapter: Any, face: Face, other: Body, reach: float) -> bool:
    """Whether a face touches a body where their boxes meet.

    Nine points of the box the face shares with the body (its centre and
    half way to its corners) are moved onto the face and a hair outward,
    along the face's normal there (a curved face's: its nearest triangle's):
    a long face touching a narrow body (a member lapped across another) has
    none of its twelve spread samples there.
    """
    lo = [max(face.box[k], other.box[k]) - reach for k in range(3)]
    hi = [min(face.box[k + 3], other.box[k + 3]) + reach for k in range(3)]
    if any(h < low for low, h in zip(lo, hi, strict=True)):
        return False
    if not face.planar:
        samples_of(adapter, face)  # its triangles and their normals
    centre = [(low + h) / 2.0 for low, h in zip(lo, hi, strict=True)]
    corners = [
        [centre[k] + (hi[k] - lo[k]) / 4.0 * sign[k] for k in range(3)]
        for sign in [(sx, sy, sz) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    ]
    for target in [centre, *corners]:
        on = as_list(
            adapter._attempt(
                lambda t=target: face.ref.GetClosestPointOn(t[0], t[1], t[2]),
                default=None,
            )
        )
        if len(on) < 3:
            continue
        point: Vector = (float(on[0]), float(on[1]), float(on[2]))
        out = (
            face.normal
            if face.planar
            else _normals_at(face.tess or [], face.norms or [], [point], face.normal)[0]
        )
        if not any(abs(c) > 1e-12 for c in out):
            continue
        probe: Vector = (
            point[0] + out[0] * PROBE_M,
            point[1] + out[1] * PROBE_M,
            point[2] + out[2] * PROBE_M,
        )
        if _touches(adapter, probe, other, reach):
            return True
    return False


def _root(parent: dict[str, str], name: str) -> str:
    """The name standing for ``name``'s group."""
    while parent[name] != name:
        parent[name] = parent[parent[name]]
        name = parent[name]
    return name


def side_contacts(
    adapter: Any,
    bodies: list[Body],
    groups: list[list[str]],
    apart: set[str],
    reach: float,
) -> set[tuple[str, str]]:
    """Links between groups through faces other than member ends.

    A strap or face plate long enough to count as a member touches its post
    only with its broad face, a member lapped onto another only with its
    side, a round bar lapped on a plate only along a line: probed at their
    ends alone they looked loose (two face plates on a door post, seen
    live). Each body named in ``apart`` is tried against each nearby body
    of another group: its faces other than member ends toward that body,
    and that body's flat faces toward it (``_face_touches``), within at most
    ``SIDE_REACH_M``. Groups joined on the way aren't tried again.
    """
    reach = min(reach, SIDE_REACH_M)
    parent = {body.name: body.name for body in bodies}
    for names in groups:
        for name in names[1:]:
            parent[_root(parent, name)] = _root(parent, names[0])
    links: set[tuple[str, str]] = set()
    for body in bodies:
        if body.name not in apart:
            continue
        read_faces(adapter, body)
        ends = {id(face) for face in body.ends} if body.member else set()
        own = [face for face in body.faces or [] if id(face) not in ends]
        for other in bodies:
            if (
                other is body
                or _root(parent, body.name) == _root(parent, other.name)
                or not boxes_near(body.box, other.box, reach)
            ):
                continue
            flat = [face for face in read_faces(adapter, other) if face.planar]
            if any(_face_touches(adapter, face, other, reach) for face in own) or any(
                _face_touches(adapter, face, body, reach) for face in flat
            ):
                links.add(_pair(body.name, other.name))
                parent[_root(parent, body.name)] = _root(parent, other.name)
    return links


def contacts(
    adapter: Any, bodies: list[Body], reach: float, only: set[str] | None = None
) -> tuple[set[tuple[str, str]], list[dict[str, Any]]]:
    """Which bodies touch, and which member ends aren't closed.

    Every member's end faces are probed (flat or coped), and every face of
    a body that isn't a member (a plate, a cap, a short stub), each at its
    sample points moved ``PROBE_M`` outward (a curved face along its own
    normal at each point). A face touches a body when a probe point lies
    within ``reach`` of it. A hollow end whose ring lands only in part is
    closed only when its opening is covered too: five points across the
    opening must find a body within ``OPENING_REACH_M`` (the weld bridges
    that), or it is partly open, like a column top a sloped beam covers a
    sixth of (seen live). A ring landing whole is closed: its bore is
    covered, or joined to another tube's (a mitre).

    Returns:
        tuple: Touching pairs, and the ends not closed: ``body``,
        ``at_mm``, ``normal``, ``hollow``, ``covered_percent`` (of the ring:
        0 for an end touching nothing) and, for a hollow end whose ring
        lands, ``opening_covered_percent``.
    """
    links: set[tuple[str, str]] = set()
    open_ends = []
    opening_reach = max(reach, OPENING_REACH_M)
    for body in bodies:
        if only is not None and body.name not in only:
            continue
        read_faces(adapter, body)
        # A member is probed at its ends (flat or coped); anything else (a
        # plate, a cap, a short stub) on every face, curved ones too.
        probed = body.ends if body.member else list(body.faces or [])
        near = [
            o
            for o in bodies
            if o is not body and boxes_near(body.box, o.box, opening_reach)
        ]
        for face in probed:
            samples, touching, hits = _probe(adapter, face, near, reach)
            links.update(_pair(body.name, name) for name in hits)
            if not (body.member and samples):
                continue
            hollow = is_hollow(adapter, face)
            ring = touching / len(samples)
            entry: dict[str, Any] | None = None
            if ring < CLOSED_SHARE:
                entry = {"covered_percent": round(100.0 * ring)}
            elif hollow and ring < FULL_RING:
                # A ring landing whole is closed: on a face or plate it covers
                # the bore, on another tube's ring (a mitre, a butt splice) the
                # bores join. Only a ring partly landed has its bore probed.
                probes = opening_points(face, samples)
                covered = sum(
                    1
                    for p in probes
                    if any(_touches(adapter, p, o, opening_reach) for o in near)
                )
                if covered < OPENING_SHARE * len(probes):
                    entry = {
                        "covered_percent": round(100.0 * ring),
                        "opening_covered_percent": round(100.0 * covered / len(probes)),
                    }
            if entry is not None:
                centre = tuple(
                    sum(s[k] for s in samples) / len(samples) for k in range(3)
                )
                open_ends.append(
                    {
                        "body": body.name,
                        "at_mm": mm(centre),
                        "normal": [round(c, 3) for c in face.normal],
                        "hollow": hollow,
                        **entry,
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

    def grouped() -> list[list[str]]:
        names = [b.name for b in bodies]
        if only is not None:
            # Outside the subset only the bodies something in it touches count.
            touched = {n for link in links for n in link}
            names = [n for n in names if n in only or n in touched]
        return groups_of(names, links)

    groups = grouped()
    # Bodies outside the largest group (when checking some: those touching
    # nothing) may touch only with a member's side or a small patch of a
    # face: probe those faces closely.
    if only is None:
        largest = max(groups, key=len) if groups else []
        apart = {n for g in groups if g is not largest for n in g}
    else:
        touched = {n for link in links for n in link}
        apart = {n for n in only if n not in touched}
    if apart and len(bodies) > 1:
        links |= side_contacts(adapter, bodies, groups, apart, contact_mm / 1000.0)
        groups = grouped()
    loose = [g[0] for g in groups if len(g) == 1 and (only is None or g[0] in only)]
    if len(bodies) < 2:
        loose = []
    for end in open_ends:
        end.pop("face", None)
    untouched = [e for e in open_ends if e["covered_percent"] == 0]
    fully_open = [e for e in untouched if e["hollow"]]
    free = [e for e in untouched if not e["hollow"]]
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
        "free_ends": {"count": len(free), "ends": free[:listed]},
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
    """The member end face nearest each point, and the points no end face is near.

    A point is near an end when it lies in the end's outline box and within
    ``reach_mm`` of its plane: the middle of a tube's end, in its opening,
    is 46 mm from the ring of an SHS 100x4 but on its plane (such an exact
    point was refused when the ring itself was measured, seen live).
    """
    reach = reach_mm / 1000.0
    chosen: list[Face] = []
    missed = []
    for point_mm in points_mm:
        point = tuple(c / 1000.0 for c in point_mm)
        best: tuple[tuple[float, float], Face] | None = None
        for body in bodies:
            if not in_box(point, body.box, reach):
                continue
            read_faces(adapter, body)
            for face in body.ends if body.member else []:
                # a coped (curved) end takes no cap
                if not face.planar or not in_box(point, face.box, reach):
                    continue
                samples = samples_of(adapter, face)
                if not samples:
                    continue
                distance = abs(dot(sub(point, samples[0]), face.normal))
                # Flush ends side by side are all on the point's plane: the
                # one whose middle is nearest across it is the one meant.
                middle = [(face.box[k] + face.box[k + 3]) / 2.0 for k in range(3)]
                key = (round(distance, 6), math.dist(point, middle))
                if distance <= reach and (best is None or key < best[0]):
                    best = (key, face)
        if best is None:
            missed.append(point_mm)
        elif best[1] not in chosen:
            chosen.append(best[1])
    return chosen, missed
