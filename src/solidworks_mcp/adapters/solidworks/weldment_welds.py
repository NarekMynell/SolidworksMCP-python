"""Shop welds where a weldment's bodies meet: cosmetic fillet weld beads.

Each piece of a bolted structure is welded in the shop, so inside a part
every joint is a weld: a member's end landing on a plate or another member,
and a plate's edge landing on a member or a plate. ``create_weld_beads``
finds those joints the way ``check_joints`` does, puts a cosmetic fillet
bead on each, and reports the weld length SolidWorks measures, for costing
and for the drawing's weld note.

Checked live on SolidWorks 2025 SP4 before it was written:

- ``IFeatureManager.InsertCosmeticWeldBead2`` with the face sets as arrays
  (``VT_DISPATCH`` or ``VT_VARIANT``) made nothing; ``InsertCosmeticWeldBead``
  with the weld-from faces selected under mark 4 and the weld-to faces under
  mark 8 made the bead.
- The weld-from faces of a member's end are the faces across its outer
  loop's edges (a 100x100x5 tube: four flats and four corner radii): the
  bead ran all round the column on its base plate, 382.8 mm (4 x 80 + four
  quarter circles of R10), the length the weld folder's ``TotalLength``
  gives. The bore's faces, across the inner loop, are left out: nobody
  welds inside a tube.
- SolidWorks files the beads in one weld sub-folder per size under the Weld
  Folder (``CosmeticWeldSubFolder`` under ``CosmeticWeldCutList``), each
  folder's ``TotalLength`` the sum of its beads' reference edges (TR-02 truss,
  55 beads in four folders, a3 to a6, within 0.02 mm). A bead's length is the
  change of every folder's total together: one folder's total, read before a
  bead of another size went into another, gave TR-02 lengths of -4222 mm.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, cast

from .. import sw_type_info as _sw_type_info
from ..base import AdapterResult
from .com_utils import as_list
from .weldment_joints import (
    PROBE_M,
    Body,
    Face,
    _pair,
    _probe,
    boxes_near,
    contacts,
    in_box,
    read_bodies,
    read_faces,
    samples_of,
)
from .weldments import _MM_PER_UNIT, _SW_UNITS_LINEAR, _named_bodies, _need_part

#: Selection marks ``InsertCosmeticWeldBead`` reads: weld from, weld to.
MARK_FROM, MARK_TO = 4, 8
#: ``IFeature::GetTypeName2`` of the Weld Folder and of its sub-folders.
WELD_FOLDER, WELD_SUB_FOLDER = "CosmeticWeldCutList", "CosmeticWeldSubFolder"
#: A fillet weld's leg is its throat ``a`` times root 2 (EN ISO 2553).
LEG_PER_THROAT = math.sqrt(2.0)
#: Bodies this close (m) touch: a joint's faces are coincident.
REACH_M = 0.0005
#: A face lands on a body when at least this share of its points touch it.
LANDS_SHARE = 0.25


@dataclass
class Seam:
    """Where one body lands on another: the face that lands, and the faces under it."""

    from_body: str
    to_body: str
    face: Face
    to_faces: list[Any] = field(default_factory=list)


def _flagged(obj: Any, interface: str) -> Any:
    return _sw_type_info.flagged(obj, interface)


def outer_neighbours(adapter: Any, face: Face) -> list[Any]:
    """The faces across a face's outer loop: a member end's sides, a plate edge's faces."""
    loop = adapter._attempt(lambda: face.ref.GetFirstLoop(), default=None)
    edges: list[Any] = []
    while loop is not None:
        loop = _flagged(loop, "ILoop2")
        if adapter._attempt(lambda lp=loop: bool(lp.IsOuter()), default=False):
            edges = as_list(
                adapter._attempt(lambda lp=loop: lp.GetEdges(), default=None)
            )
            break
        loop = adapter._attempt(lambda lp=loop: lp.GetNext(), default=None)
    found: list[Any] = []
    for raw in edges:
        edge = _flagged(raw, "IEdge")
        for adjacent in as_list(
            adapter._attempt(lambda e=edge: e.GetTwoAdjacentFaces2(), default=None)
        ):
            other = _flagged(adjacent, "IFace2")
            same = adapter._attempt(lambda o=other: bool(o.IsSame(face.ref)), False)
            if same or any(
                adapter._attempt(lambda o=other, f=f: bool(o.IsSame(f)), False)
                for f in found
            ):
                continue
            found.append(other)
    return found


def landing_faces(adapter: Any, face: Face, other: Body, reach: float) -> list[Any]:
    """The faces of ``other`` that a face's points land on."""
    samples = samples_of(adapter, face)
    normals = face.sample_normals or [face.normal] * len(samples)
    lifted = [
        tuple(s[k] + n[k] * PROBE_M for k in range(3))
        for s, n in zip(samples, normals, strict=False)
    ]
    hit = []
    for candidate in read_faces(adapter, other):
        for point in lifted:
            if not in_box(point, candidate.box, reach):
                continue
            closest = as_list(
                adapter._attempt(
                    lambda c=candidate, p=point: c.ref.GetClosestPointOn(*p),
                    default=None,
                )
            )
            if (
                len(closest) >= 3
                and math.dist(point, [float(v) for v in closest[:3]]) <= reach
            ):
                hit.append(candidate.ref)
                break
    return hit


def _welded_already(
    adapter: Any, seams: list[Seam], lands: str, under: str, faces: list[Any]
) -> bool:
    """Whether ``under``'s end, landing on ``lands`` at one of ``faces``, is welded already."""
    return any(
        seam.from_body == under
        and seam.to_body == lands
        and any(
            adapter._attempt(lambda f=f, s=seam: bool(f.IsSame(s.face.ref)), False)
            for f in faces
        )
        for seam in seams
    )


def find_seams(
    adapter: Any, bodies: list[Body], reach: float, only: set[str] | None = None
) -> tuple[list[Seam], list[tuple[str, str]]]:
    """Every joint to weld, and the touching pairs no face lands in (members lapped side by side).

    A member's end landing on a body is a seam from that end. Bodies that
    touch otherwise (a plate's edge on a member, a stiffener on a plate) are
    welded from the smaller of the faces that touch.
    """
    by_name = {b.name: b for b in bodies}
    seams: list[Seam] = []
    done: set[tuple[str, str]] = set()
    for body in bodies:
        read_faces(adapter, body)
        if not body.member:
            continue
        near = [
            o for o in bodies if o is not body and boxes_near(body.box, o.box, reach)
        ]
        for end in body.ends:
            _samples, _touching, hits = _probe(adapter, end, near, reach)
            for name in sorted(hits):
                if only is not None and not {body.name, name} & only:
                    continue
                under = landing_faces(adapter, end, by_name[name], reach)
                # Two mitred ends land on each other: one weld, not two.
                if under and not _welded_already(
                    adapter, seams, body.name, name, under
                ):
                    seams.append(Seam(body.name, name, end, under))
                    done.add(_pair(body.name, name))
    links, _open = contacts(adapter, bodies, reach)
    apart: list[tuple[str, str]] = []
    for first, second in sorted(links - done):
        if only is not None and not {first, second} & only:
            continue
        a, b = by_name[first], by_name[second]
        landing = []
        for lands, other in ((a, b), (b, a)):
            for face in read_faces(adapter, lands):
                if not face.planar:
                    continue
                samples, touching, _hits = _probe(adapter, face, [other], reach)
                if samples and touching:
                    landing.append(
                        (touching / len(samples), -face.area, face, lands, other)
                    )
        # The face most of whose points land: a plate's edge on a member, not
        # the plate's side that only meets the member along one line.
        best = max(landing, key=lambda item: item[:2], default=None)
        under = (
            landing_faces(adapter, best[2], best[4], reach) if best is not None else []
        )
        if best is None or best[0] < LANDS_SHARE or not under:
            apart.append((first, second))
            continue
        seams.append(Seam(best[3].name, best[4].name, best[2], under))
    return seams, apart


def bead_features(adapter: Any, model: Any) -> list[Any]:
    """The part's cosmetic weld beads, in the tree and inside its folders."""
    found: list[Any] = []
    feature = adapter._attempt(lambda: model.FirstFeature(), default=None)
    while feature is not None:
        feature = _flagged(feature, "IFeature")
        stack = [feature]
        while stack:
            current = stack.pop()
            kind = adapter._attempt(lambda c=current: str(c.GetTypeName2()), default="")
            if kind == "CosmeticWeldBead":
                found.append(current)
            child = adapter._attempt(lambda c=current: c.GetFirstSubFeature(), None)
            while child is not None:
                child = _flagged(child, "IFeature")
                stack.append(child)
                child = adapter._attempt(lambda c=child: c.GetNextSubFeature(), None)
        feature = adapter._attempt(lambda f=feature: f.GetNextFeature(), default=None)
    return found


def weld_total_mm(adapter: Any, model: Any, to_mm: float) -> float | None:
    """Every cosmetic weld bead's length in the part (mm), as SolidWorks adds it up.

    The Weld Folder's sub-folders (one per bead size) added up: 0 before the
    first bead, ``None`` when a sub-folder can't be read.

    Args:
        adapter: The adapter (for ``_attempt``).
        model: The part.
        to_mm: Millimetres in one document length unit (``TotalLength`` is in
            document units, like the cut list's lengths).
    """
    feature = adapter._attempt(lambda: model.FirstFeature(), default=None)
    while feature is not None:
        feature = _flagged(feature, "IFeature")
        kind = adapter._attempt(lambda f=feature: str(f.GetTypeName2()), default="")
        if kind == WELD_FOLDER:
            return _sub_folders_mm(adapter, feature, to_mm)
        feature = adapter._attempt(lambda f=feature: f.GetNextFeature(), default=None)
    return 0.0


def _sub_folders_mm(adapter: Any, folder: Any, to_mm: float) -> float | None:
    """The weld sub-folders' total lengths added up (mm), or ``None``."""
    total = 0.0
    child = adapter._attempt(lambda: folder.GetFirstSubFeature(), default=None)
    while child is not None:
        child = _flagged(child, "IFeature")
        if (
            adapter._attempt(lambda c=child: str(c.GetTypeName2()), default="")
            == WELD_SUB_FOLDER
        ):
            length = adapter._attempt(
                lambda c=child: float(
                    _flagged(
                        c.GetSpecificFeature2(), "ICosmeticWeldBeadFolder"
                    ).TotalLength
                ),
                default=None,
            )
            if length is None:
                return None
            total += length
        child = adapter._attempt(lambda c=child: c.GetNextSubFeature(), default=None)
    return round(total * to_mm, 1)


def _select(model: Any, faces: list[Any], mark: int) -> bool:
    """Add faces to the selection under ``mark``; whether every one took."""
    manager = _flagged(model.SelectionManager, "ISelectionMgr")
    ok = True
    for face in faces:
        data = _flagged(manager.CreateSelectData(), "ISelectData")
        data.Mark = mark
        ok = bool(face.Select4(True, data)) and ok
    return ok


def weld(
    adapter: Any, model: Any, seam: Seam, leg_m: float, name: str
) -> tuple[Any | None, str | None]:
    """One cosmetic fillet bead along a seam; the feature, or why there is none."""
    sides = outer_neighbours(adapter, seam.face)
    if not sides:
        return None, "the landing face has no outer edges to weld along"
    model.ClearSelection2(True)
    if not (
        _select(model, sides, MARK_FROM) and _select(model, seam.to_faces, MARK_TO)
    ):
        model.ClearSelection2(True)
        return None, "SolidWorks wouldn't select the faces to weld"
    manager = _flagged(model.FeatureManager, "IFeatureManager")
    made = adapter._attempt(lambda: manager.InsertCosmeticWeldBead(leg_m), default=None)
    model.ClearSelection2(True)
    if made is None:
        return None, "SolidWorks made no weld bead along the joint"
    feature = _flagged(made, "IFeature")
    adapter._attempt(lambda: setattr(feature, "Name", name))
    return feature, None


class SolidWorksWeldmentWeldsMixin:
    """Cosmetic fillet weld beads on a weldment's joints."""

    @staticmethod
    def _adapter(obj: Any) -> Any:
        """Return the runtime adapter object for dynamic attribute access."""
        return cast(Any, obj)

    async def create_weld_beads(
        self, params: dict[str, Any]
    ) -> AdapterResult[dict[str, Any]]:
        """Put a cosmetic fillet weld on every joint of the weldment, or of the members named.

        Args:
            params: ``throat_mm`` (the fillet's throat ``a``) and, optionally,
                ``members`` (only their joints).

        Returns:
            AdapterResult[dict[str, Any]]: ``beads``, ``throat_mm``,
            ``leg_mm``, ``total_length_mm`` (every bead in the part),
            ``welds`` (``from``, ``to``, ``length_mm``), ``welded_already``
            (joints an earlier call welded), ``failed`` and ``not_welded``
            (pairs touching only side by side).
        """
        adapter = self._adapter(self)
        if wrong := _need_part(adapter, "create_weld_beads"):
            return wrong

        def run() -> dict[str, Any]:
            model = adapter.currentModel
            part = _flagged(model, "IPartDoc")
            manager = _flagged(model.SketchManager, "ISketchManager")
            if adapter._attempt(lambda: manager.ActiveSketch) is not None:
                raise ValueError("A sketch is open; close it with exit_sketch first")
            throat = float(params["throat_mm"])
            leg_m = throat * LEG_PER_THROAT / 1000.0
            only = None
            if params.get("members"):
                only = set(_named_bodies(adapter, model, part, list(params["members"])))
            bodies = read_bodies(adapter, part)
            seams, apart = find_seams(adapter, bodies, REACH_M, only)
            # Beads there already (an earlier call) count in the part's total
            # and their joints are not welded twice.
            there = bead_features(adapter, model)
            names = {
                str(adapter._attempt(lambda f=f: f.Name, default="")) for f in there
            }
            unit = adapter._attempt(
                lambda: int(model.GetUserPreferenceIntegerValue(_SW_UNITS_LINEAR)),
                default=0,
            )
            to_mm = _MM_PER_UNIT.get(int(unit or 0), 1.0)
            total = weld_total_mm(adapter, model, to_mm)
            welds: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []
            existing: list[dict[str, str]] = []
            for seam in seams:
                label = f"Weld {seam.from_body} to {seam.to_body}"
                if label in names:
                    existing.append({"from": seam.from_body, "to": seam.to_body})
                    continue
                feature, why = weld(adapter, model, seam, leg_m, label)
                if feature is None:
                    failed.append(
                        {"from": seam.from_body, "to": seam.to_body, "reason": why}
                    )
                    continue
                now = weld_total_mm(adapter, model, to_mm)
                length = None if now is None or total is None else round(now - total, 1)
                total = now
                welds.append(
                    {"from": seam.from_body, "to": seam.to_body, "length_mm": length}
                )
            adapter._attempt(lambda: model.GraphicsRedraw2())
            data: dict[str, Any] = {
                "beads": len(welds),
                "throat_mm": throat,
                "leg_mm": round(throat * LEG_PER_THROAT, 1),
                "total_length_mm": total,
                "welds": welds,
            }
            if existing:
                data["welded_already"] = existing
            if failed:
                data["failed"] = failed
            if apart:
                data["not_welded"] = [
                    {"bodies": list(pair), "reason": "they touch side by side only"}
                    for pair in apart
                ]
            return data

        return cast(
            AdapterResult[dict[str, Any]],
            adapter._handle_com_operation("create_weld_beads", run),
        )
