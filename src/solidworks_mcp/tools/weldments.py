"""Weldment tools: structural members, the profile library, joints, end caps and the cut list.

What a steel fabricator builds a frame, truss or platform with: one call puts
a profile (IPE, HEB, SHS, RHS, UPN, angle...) from SolidWorks' weldment
library on many sketch segments, the joint check measures where members run
into each other or end in the air, end caps close hollow ends, and the cut
list gives every member's length, end angles, quantity and mass for the shop.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..adapters.base import SolidWorksAdapter
from ..adapters.solidworks.weldment_cuts import check_cut
from ..adapters.solidworks.weldment_plates import check_plate, plate_frame
from .input_compat import CompatInput
from .tool_boundary import (
    InvalidInputError,
    normalize_input,
    require_capability,
    require_success,
    with_tool_boundary,
)


class ListWeldmentProfilesInput(CompatInput):
    """Input schema for reading the weldment profile library.

    Attributes:
        standard (str | None): One standard's types and sizes.
        profile_type (str | None): One type's sizes (needs ``standard``).
        query (str | None): Words to search type and size names for.
        limit (int): The most search results.
    """

    standard: str | None = Field(
        default=None,
        description="A standard's folder, e.g. 'DIN', 'BSI', 'iso', 'ansi'",
    )
    profile_type: str | None = Field(
        default=None,
        description="A profile type of that standard, e.g. 'ipe', 'hea', 'shs'",
    )
    query: str | None = Field(
        default=None,
        description="Search words over every type and size, e.g. 'HEB400', "
        "'shs 250x250', 'ipe 300'",
    )
    limit: int = Field(default=50, ge=1, le=500, description="The most search results")

    @model_validator(mode="after")
    def _type_needs_standard(self) -> ListWeldmentProfilesInput:
        """A type is looked up within its standard."""
        if self.profile_type and not self.standard:
            raise ValueError("profile_type needs standard")
        return self


#: One straight segment: start and end point, mm.
Segment = list[float]
#: The most segments one call takes.
MAX_SEGMENTS = 2000


def check_segments(segments: list[Any], where: str = "segment") -> list[list[float]]:
    """Segments as six numbers each with a length, or ``ValueError`` naming the bad one."""
    checked = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, (list, tuple)) or len(segment) != 6:
            raise ValueError(
                f"{where} {index} isn't six numbers; give x1, y1, z1, x2, y2, z2"
            )
        try:
            values = [float(v) for v in segment]
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{where} {index} has a value that isn't a number"
            ) from error
        if math.dist(values[:3], values[3:]) < 0.001:
            raise ValueError(f"{where} {index} has no length")
        checked.append(values)
    return checked


def load_segments(path: str) -> list[list[float]]:
    """The segments a JSON file holds: a list of six-number lists, or ``{"segments": [...]}``.

    Raises:
        InvalidInputError: The file is missing, isn't JSON, or holds anything else.
    """
    if not os.path.isfile(path):
        raise InvalidInputError(f"segments_file {path!r} doesn't exist")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as error:
        raise InvalidInputError(
            f"segments_file {path!r} isn't readable JSON: {error}"
        ) from error
    if isinstance(data, dict):
        data = data.get("segments")
    if not isinstance(data, list) or not data:
        raise InvalidInputError(
            f"segments_file {path!r} must hold a list of [x1, y1, z1, x2, y2, z2] "
            'segments, or {"segments": [...]}'
        )
    if len(data) > MAX_SEGMENTS:
        raise InvalidInputError(
            f"segments_file {path!r} has {len(data)} segments; one call takes "
            f"{MAX_SEGMENTS}: split it"
        )
    try:
        return check_segments(data, "segments_file entry")
    except ValueError as error:
        raise InvalidInputError(f"{error} ({path})") from error


class CreateStructuralMembersInput(CompatInput):
    """Input schema for a structural member feature.

    Attributes:
        define (str): How the new 3D sketch is tied down.
        folder (str | None): The feature-tree folder for the new features.
        size (str): The profile size, e.g. ``IPE300``.
        standard (str | None): The library standard, e.g. ``DIN``.
        profile_type (str | None): The library type, e.g. ``ipe``.
        profile_path (str | None): A ``.sldlfp`` of your own instead.
        segments (list[list[float]] | None): Lines to draw, mm.
        sketch (str | None): An existing sketch to use instead.
        segment_names (list[str] | None): Only these segments of it.
        corner_treatment (str): Where segments of one chain meet.
        up (list[float] | None): Where each profile's height points.
        rotation (float): Profile turn about the member axis, degrees.
        mirror_profile (str): Mirror the profile about an axis.
        merge_arcs (bool): One body for tangent arc segments.
        grouping (str): How segments are grouped.
        name (str | None): The feature's name.
        sketch_name (str | None): The new 3D sketch's name.
    """

    size: str = Field(
        min_length=1,
        description="Profile size as the library names it, e.g. 'IPE300', 'HE400B', "
        "'RHS100x100x5' (list_weldment_profiles finds it)",
    )
    standard: str | None = Field(
        default=None, description="Library standard, e.g. 'DIN'"
    )
    profile_type: str | None = Field(
        default=None, description="Library profile type, e.g. 'ipe', 'hea', 'shs'"
    )
    profile_path: str | None = Field(
        default=None,
        description="A .sldlfp profile of your own instead of standard and profile_type",
    )
    segments: list[Segment] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SEGMENTS,
        description="Member axes to draw as a new 3D sketch, each "
        "[x1, y1, z1, x2, y2, z2] in mm; draw a continuous member as one segment",
    )
    segments_file: str | None = Field(
        default=None,
        description="A .json file of segments instead of segments: a list of "
        '[x1, y1, z1, x2, y2, z2] in mm (or {"segments": [...]}), such as a '
        "script writes for a lattice",
    )
    sketch: str | None = Field(
        default=None,
        description="An existing sketch (2D or 3D) whose lines and arcs to use instead",
    )
    segment_names: list[str] | None = Field(
        default=None,
        description="Only these segments of sketch, e.g. ['Line1', 'Line4']",
    )
    corner_treatment: Literal["miter", "butt1", "butt2", "none"] = Field(
        default="miter",
        description="Where segments of one chain meet: 'miter', 'butt1', 'butt2' or 'none'",
    )
    up: list[float] | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="Which way each profile's height (its web, the 150 of an "
        "RHS 150x100) points, [x, y, z], taken square to each member. Default "
        "model y: beams, rafters and braces upright, sloped ones not rolled. "
        "Purlins square to a roof: the roof's normal; columns: the way their "
        "height faces in plan, e.g. [1, 0, 0] (up can't run along a member)",
    )
    rotation: float = Field(
        default=0.0,
        description="A further turn of the profile about the member's axis, "
        "degrees, after up (45 sets an angle's legs diagonal)",
    )
    mirror_profile: Literal["none", "horizontal", "vertical"] = Field(
        default="none",
        description="Mirror the profile about its horizontal or vertical axis",
    )
    merge_arcs: bool = Field(
        default=False, description="Make tangent arc segments one body"
    )
    grouping: Literal["auto", "single", "each"] = Field(
        default="auto",
        description="'auto' groups segments joined end to end (mitred corners); "
        "'single' one group; 'each' one group per segment",
    )
    name: str | None = Field(default=None, description="Name for the new feature")
    sketch_name: str | None = Field(
        default=None,
        description="Name for the new 3D sketch drawn from segments; default "
        "'<name> skeleton'",
    )
    define: Literal["dimensions", "none"] = Field(
        default="dimensions",
        description="'dimensions' ties every node of the new 3D sketch to the "
        "Right, Top and Front planes (its x, y and z as dimensions the user can "
        "edit; a Fix relation where SolidWorks refuses one), so the skeleton is "
        "fully defined and nothing moves when dragged; 'none' leaves it under "
        "defined, for a quick draft",
    )
    folder: str | None = Field(
        default=None,
        description="Put the member feature and its sketch in this feature-tree "
        "folder (made if missing), e.g. 'Tower' or 'Bracing'",
    )

    @model_validator(mode="after")
    def _one_profile_one_path(self) -> CreateStructuralMembersInput:
        """A profile from the library or a file, on segments or a sketch."""
        if not self.profile_path and not (self.standard and self.profile_type):
            raise ValueError("give standard and profile_type, or profile_path")
        given = [
            x is not None for x in (self.segments, self.segments_file, self.sketch)
        ]
        if sum(given) != 1:
            raise ValueError("give segments, segments_file or sketch, one of them")
        check_segments(self.segments or [])
        if self.segment_names and not self.sketch:
            raise ValueError("segment_names needs sketch")
        if self.up is not None and math.hypot(*self.up) < 1e-9:
            raise ValueError("up is a direction: give a vector of some length")
        return self


class CreateTrimExtendInput(CompatInput):
    """Input schema for trimming or extending structural members.

    Attributes:
        members (list[str]): The members to trim or extend.
        boundary (list[str]): The members they are trimmed to.
        corner_type (str): ``end_trim``, ``miter``, ``butt1`` or ``butt2``.
        cut (str): ``simple`` or ``coped``.
        weld_gap_mm (float): Gap left for the weld, mm.
        allow_extension (bool): Extend members that stop short.
        name (str | None): The feature's name.
    """

    members: list[str] = Field(
        min_length=1,
        description="Members to trim or extend: member feature names (all its "
        "bodies, e.g. 'Joists IPE160') or body names (e.g. 'Joists IPE160[3]')",
    )
    boundary: list[str] = Field(
        min_length=1,
        description="What they are trimmed to, the same kind of names, e.g. "
        "['Main beams IPE240']",
    )
    corner_type: Literal["end_trim", "miter", "butt1", "butt2"] = Field(
        default="end_trim",
        description="'end_trim' cuts every member at the boundary; 'miter', "
        "'butt1' and 'butt2' join one member to one boundary member at a corner",
    )
    cut: Literal["simple", "coped"] = Field(
        default="simple",
        description="'simple' cuts square at the boundary's nearest flat face; "
        "'coped' cuts to the boundary's shape (a joist notched round a beam's "
        "flanges, tops flush)",
    )
    weld_gap_mm: float = Field(
        default=0.0, ge=0, le=100, description="Gap left for the weld at each cut, mm"
    )
    allow_extension: bool = Field(
        default=True, description="Extend members that stop short of the boundary"
    )
    name: str | None = Field(
        default=None, description="Name for the new feature, e.g. 'Joists to beams'"
    )
    folder: str | None = Field(
        default=None,
        description="Feature-tree folder; default the last folder holding the "
        "members or boundary (a new numbered one when moving it there would put "
        "it before a later feature)",
    )

    @model_validator(mode="after")
    def _a_corner_joins_two(self) -> CreateTrimExtendInput:
        """A miter or butt corner is one member and one boundary member."""
        if self.corner_type != "end_trim" and (
            len(self.members),
            len(self.boundary),
        ) != (1, 1):
            raise ValueError(
                f"a {self.corner_type} corner joins one member to one boundary "
                "member; use end_trim for more"
            )
        return self


class CheckJointsInput(CompatInput):
    """Input schema for measuring a weldment's joints.

    Attributes:
        members (list[str] | None): Only these member features or bodies.
        overlap_mm3 (float): Shared volume that still counts as touching.
        contact_mm (float): How near an end must come to count as closed.
        max_listed (int): The most items each list gives.
    """

    members: list[str] | None = Field(
        default=None,
        description="Only these member features or bodies (e.g. 'Leg braces'): "
        "their overlaps, ends and contacts; every body when omitted",
    )
    overlap_mm3: float = Field(
        default=1.0,
        ge=0,
        description="Bodies sharing at most this volume, mm3, touch rather than overlap",
    )
    contact_mm: float = Field(
        default=0.5,
        gt=0,
        le=20,
        description="How near an end must come to another body to be closed, mm; "
        "raise it above a weld gap a trim left",
    )
    max_listed: int = Field(
        default=25,
        ge=1,
        le=500,
        description="The most overlaps, open ends and loose bodies listed (counts are complete)",
    )


class CreateEndCapsInput(CompatInput):
    """Input schema for end caps on structural members.

    Attributes:
        ends (list[list[float]] | None): Points at the ends to cap, mm.
        open_ends (bool): Cap every open member end instead.
        thickness_mm (float): The cap's thickness.
        placement (str): ``inward``, ``outward`` or ``internal``.
        inset_mm (float): Inset of the cap's edges from the profile's outside.
        depth_mm (float): An internal cap's depth inside the end.
        contact_mm (float): How near an end must come to something to be closed.
        name (str | None): The feature's name.
    """

    ends: list[list[float]] | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Points [x, y, z] mm at the member ends to cap; each takes the "
        "member end face nearest it, within 20 mm",
    )
    open_ends: bool = Field(
        default=False,
        description="Cap every member end that touches nothing (check_joints' open ends)",
    )
    thickness_mm: float = Field(
        default=5.0, gt=0, le=100, description="Cap thickness, mm"
    )
    placement: Literal["inward", "outward", "internal"] = Field(
        default="inward",
        description="'inward' flush with the end (the member is shortened by the "
        "thickness, so the overall size stays); 'outward' on the end (adds the "
        "thickness); 'internal' inside the tube, depth_mm in",
    )
    inset_mm: float = Field(
        default=0.0,
        ge=0,
        le=50,
        description="Inset of the cap's edges from the profile's outside, mm (room "
        "for a fillet weld)",
    )
    depth_mm: float = Field(
        default=0.0,
        ge=0,
        le=500,
        description="'internal' caps: depth inside the end, mm",
    )
    contact_mm: float = Field(
        default=0.5,
        gt=0,
        le=20,
        description="With open_ends: how near an end must come to another body to be closed, mm",
    )
    name: str | None = Field(default=None, description="Name for the new feature")

    @model_validator(mode="after")
    def _ends_or_open_ends(self) -> CreateEndCapsInput:
        """Ends named by points, or every open end: one of them."""
        if bool(self.ends) == bool(self.open_ends):
            raise ValueError("give ends (points) or open_ends: true, one of them")
        for index, point in enumerate(self.ends or []):
            if len(point) != 3:
                raise ValueError(f"end {index} isn't [x, y, z]")
        return self


#: The most plates one call takes.
MAX_PLATES = 500


class PlateHoles(BaseModel):
    """Holes of one size in a plate.

    Attributes:
        diameter (float): The hole diameter, mm.
        positions (list[list[float]]): Hole centres in the plate's x/y, mm.
    """

    model_config = ConfigDict(extra="forbid")

    diameter: float = Field(
        gt=0,
        le=500,
        description="Hole diameter, mm: a bolt's normal clearance hole is the bolt "
        "plus 2 mm (M12-M24) or 3 mm (M27 and up), EN 1090-2",
    )
    positions: list[list[float]] = Field(
        min_length=1, description="Hole centres [x, y] in the plate's own x/y, mm"
    )

    @model_validator(mode="after")
    def _pairs(self) -> PlateHoles:
        """Each position is two numbers."""
        for position in self.positions:
            if len(position) != 2:
                raise ValueError("each hole position is [x, y]")
        return self


class PlateSpec(BaseModel):
    """One plate: what it is, its outline and holes, and where it sits.

    Attributes:
        name (str): What the plate is.
        mark (str | None): The shop mark.
        thickness (float): mm.
        origin (list[float]): The plate's reference point, mm.
        normal (list[float]): The thickness direction.
        x_axis (list[float] | None): The plate's own x direction.
        outline (list[list[float]] | None): Corners in the plate's x/y, mm.
        width (float | None): A centred rectangle's width, mm.
        height (float | None): A centred rectangle's height, mm.
        holes (list[PlateHoles]): Holes, by size.
        thickness_side (str): Which way the thickness grows.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, description="What the plate is, e.g. 'Base plate', 'Splice plate'"
    )
    mark: str | None = Field(
        default=None,
        description="Shop mark, e.g. 'BP-1': names the plate, its cut-list item "
        "and its DXF",
    )
    thickness: float = Field(gt=0, le=200, description="Plate thickness, mm")
    origin: list[float] = Field(
        min_length=3,
        max_length=3,
        description="The plate's reference point [x, y, z] mm (its own x, y = 0), "
        "on the face the thickness grows from",
    )
    normal: list[float] = Field(
        min_length=3,
        max_length=3,
        description="Thickness direction [x, y, z], any length: [0, -1, 0] for a "
        "base plate under a column foot, the member's axis for a splice or cap plate",
    )
    x_axis: list[float] | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="The plate's own x direction; default: model x for a level "
        "plate, otherwise the plate's y runs up",
    )
    outline: list[list[float]] | None = Field(
        default=None,
        min_length=3,
        description="Corners [x, y] in the plate's x/y, mm, in order, the first "
        "not repeated at the end",
    )
    width: float | None = Field(
        default=None,
        gt=0,
        description="A width x height rectangle centred on origin instead of "
        "outline, mm",
    )
    height: float | None = Field(
        default=None, gt=0, description="The rectangle's height, mm"
    )
    holes: list[PlateHoles] = Field(default_factory=list, description="Holes, by size")
    thickness_side: Literal["normal", "reverse", "mid"] = Field(
        default="normal",
        description="'normal' the thickness grows along normal from origin, "
        "'reverse' against it, 'mid' half each way",
    )

    @model_validator(mode="after")
    def _shape(self) -> PlateSpec:
        """An outline or a rectangle, with every hole inside the plate."""
        if (self.outline is None) == (self.width is None or self.height is None):
            raise ValueError("give outline, or width and height")
        if self.outline is not None and any(len(p) != 2 for p in self.outline):
            raise ValueError("each outline corner is [x, y]")
        check_plate(self.model_dump())
        plate_frame(self.origin, self.normal, self.x_axis)
        return self


class CreatePlatesInput(CompatInput):
    """Input schema for plates in a weldment part.

    Attributes:
        plates (list[PlateSpec] | None): The plates.
        plates_file (str | None): A JSON file of them instead.
        folder (str | None): The feature-tree folder.
        define (str): How the plate sketches are tied down.
    """

    plates: list[PlateSpec] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_PLATES,
        description="The plates, each with name, thickness, origin, normal and "
        "outline (or width and height), holes and mark",
    )
    plates_file: str | None = Field(
        default=None,
        description="A .json file of plates instead (a list, or "
        '{"plates": [...]}), such as a script writes for many splice plates',
    )
    folder: str | None = Field(
        default="Plates",
        description="Feature-tree folder for the plates (made if missing); null for none",
    )
    define: Literal["dimensions", "none"] = Field(
        default="dimensions",
        description="'dimensions' dimensions every corner and hole of each plate "
        "from the origin and each hole's diameter (fully defined sketches); "
        "'none' leaves them under defined",
    )

    @model_validator(mode="after")
    def _one_source(self) -> CreatePlatesInput:
        """Plates or a file of them, not both."""
        if (self.plates is None) == (self.plates_file is None):
            raise ValueError("give plates or plates_file, one of them")
        return self


def _load(
    path: str, key: str, spec: type[BaseModel], most: int
) -> list[dict[str, Any]]:
    """The items a JSON file holds (a list, or ``{key: [...]}``), each checked by ``spec``.

    Raises:
        InvalidInputError: The file is missing, isn't JSON, holds too many
            items, or an item is wrong.
    """
    source = f"{key}_file"
    if not os.path.isfile(path):
        raise InvalidInputError(f"{source} {path!r} doesn't exist")
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as error:
        raise InvalidInputError(
            f"{source} {path!r} isn't readable JSON: {error}"
        ) from error
    if isinstance(data, dict):
        data = data.get(key)
    if not isinstance(data, list) or not data:
        raise InvalidInputError(
            f'{source} {path!r} must hold a list of {key}, or {{"{key}": [...]}}'
        )
    if len(data) > most:
        raise InvalidInputError(
            f"{source} {path!r} has {len(data)} {key}; one call takes {most}: split it"
        )
    items = []
    for index, item in enumerate(data):
        try:
            items.append(spec.model_validate(item).model_dump())
        except ValidationError as error:
            first = error.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            raise InvalidInputError(
                f"{source} entry {index}{' ' + where if where else ''}: "
                f"{first.get('msg')} ({path})"
            ) from error
    return items


def load_plates(path: str) -> list[dict[str, Any]]:
    """The plates a JSON file holds, each checked like ``plates``.

    Raises:
        InvalidInputError: The file is missing, isn't JSON, or a plate is wrong.
    """
    return _load(path, "plates", PlateSpec, MAX_PLATES)


#: The most cuts one call takes.
MAX_CUTS = 500


class CutSpec(BaseModel):
    """One cut in the members: holes, a cope or a notch, where it sits and how deep.

    Attributes:
        name (str): What the cut is.
        origin (list[float]): The cut's reference point, mm.
        normal (list[float]): The direction it goes through.
        x_axis (list[float] | None): The cut's own x direction.
        outline (list[list[float]] | None): The steel to remove, in its x/y.
        width (float | None): A centred rectangle's width, mm.
        height (float | None): A centred rectangle's height, mm.
        holes (list[PlateHoles]): Holes, by size.
        depth (float): How far it reaches, half each side, mm.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        description="What the cut is, e.g. 'FJ-1 end cope', 'MB-1 web holes A'",
    )
    origin: list[float] = Field(
        min_length=3,
        max_length=3,
        description="The cut's reference point [x, y, z] mm (its own x, y = 0), in "
        "the middle of the steel it cuts: on the web's centre plane for web "
        "holes or a cope",
    )
    normal: list[float] = Field(
        min_length=3,
        max_length=3,
        description="The direction the cut goes through [x, y, z], any length: "
        "square to the web for web holes and copes, along the bolts",
    )
    x_axis: list[float] | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="The cut's own x direction; default: model x for a level cut, "
        "otherwise its y runs up",
    )
    outline: list[list[float]] | None = Field(
        default=None,
        min_length=3,
        description="Corners [x, y] of the steel to remove, in the cut's x/y, mm, "
        "in order: a cope or notch, drawn past the member's end and flange",
    )
    width: float | None = Field(
        default=None,
        gt=0,
        description="A width x height opening centred on origin instead of outline, mm",
    )
    height: float | None = Field(
        default=None, gt=0, description="The opening's height, mm"
    )
    holes: list[PlateHoles] = Field(
        default_factory=list,
        description="Holes, by size, in the cut's x/y; none inside the outline",
    )
    depth: float = Field(
        gt=0,
        le=5000,
        description="How far the cut reaches, mm, half each side of origin's "
        "plane: through the web and a plate on it, short of the next member",
    )

    @model_validator(mode="after")
    def _shape(self) -> CutSpec:
        """An outline or an opening (or neither, for holes), and holes clear of it."""
        if self.outline is not None and (self.width or self.height):
            raise ValueError("give outline, or width and height, not both")
        if (self.width is None) != (self.height is None):
            raise ValueError("give width and height together")
        if self.outline is not None and any(len(p) != 2 for p in self.outline):
            raise ValueError("each outline corner is [x, y]")
        check_cut(self.model_dump())
        plate_frame(self.origin, self.normal, self.x_axis)
        return self


class CreateCutsInput(CompatInput):
    """Input schema for holes, copes and notches in weldment members.

    Attributes:
        cuts (list[CutSpec] | None): The cuts.
        cuts_file (str | None): A JSON file of them instead.
        folder (str | None): The feature-tree folder.
        define (str): How the cut sketches are tied down.
    """

    cuts: list[CutSpec] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CUTS,
        description="The cuts, each with name, origin, normal, depth and an "
        "outline (or width and height), holes, or both",
    )
    cuts_file: str | None = Field(
        default=None,
        description='A .json file of cuts instead (a list, or {"cuts": [...]}), '
        "such as a script writes for every joist end",
    )
    folder: str | None = Field(
        default="Cuts",
        description="Feature-tree folder for the cuts (made if missing); null for none",
    )
    define: Literal["dimensions", "none"] = Field(
        default="dimensions",
        description="'dimensions' dimensions every corner and hole of each cut "
        "from the origin and each hole's diameter (fully defined sketches); "
        "'none' leaves them under defined",
    )

    @model_validator(mode="after")
    def _one_source(self) -> CreateCutsInput:
        """Cuts or a file of them, not both."""
        if (self.cuts is None) == (self.cuts_file is None):
            raise ValueError("give cuts or cuts_file, one of them")
        return self


def load_cuts(path: str) -> list[dict[str, Any]]:
    """The cuts a JSON file holds, each checked like ``cuts``.

    Raises:
        InvalidInputError: The file is missing, isn't JSON, or a cut is wrong.
    """
    return _load(path, "cuts", CutSpec, MAX_CUTS)


class PatternStep(BaseModel):
    """Copies in a row: which way, how far apart, how many.

    Attributes:
        direction (list[float]): The way the copies go.
        spacing (float): Distance between instances, mm.
        count (int): Instances, the original included.
    """

    model_config = ConfigDict(extra="forbid")

    direction: list[float] = Field(
        min_length=3,
        max_length=3,
        description="[x, y, z], any length: the way the copies go from the original",
    )
    spacing: float = Field(gt=0, le=1e6, description="Distance between instances, mm")
    count: int = Field(ge=2, le=1000, description="Instances, the original included")

    @model_validator(mode="after")
    def _has_length(self) -> PatternStep:
        """A direction of some length."""
        if math.hypot(*self.direction) < 1e-9:
            raise ValueError("direction has no length")
        return self


class PatternAround(BaseModel):
    """Copies around an axis.

    Attributes:
        axis (list[float]): The axis direction.
        point (list[float]): A point on the axis, mm.
        count (int): Instances, the original included.
        angle (float): The total angle, degrees.
    """

    model_config = ConfigDict(extra="forbid")

    axis: list[float] = Field(
        min_length=3,
        max_length=3,
        description="The axis [x, y, z]; copies turn about it by the right hand",
    )
    point: list[float] = Field(
        default_factory=lambda: [0.0, 0.0, 0.0],
        min_length=3,
        max_length=3,
        description="A point on the axis, mm",
    )
    count: int = Field(ge=2, le=360, description="Instances, the original included")
    angle: float = Field(
        default=360.0,
        gt=0,
        le=360,
        description="Total angle, degrees: 360 spaces them evenly all round; less "
        "spreads them from the original to the last",
    )

    @model_validator(mode="after")
    def _has_length(self) -> PatternAround:
        """An axis of some length."""
        if math.hypot(*self.axis) < 1e-9:
            raise ValueError("axis has no length")
        return self


class PatternMirror(BaseModel):
    """A mirror plane: a main plane, or one through a point.

    Attributes:
        plane (str | None): ``Front``, ``Top`` or ``Right``.
        point (list[float] | None): A point on the plane, mm.
        normal (list[float] | None): The plane's normal, along x, y or z.
    """

    model_config = ConfigDict(extra="forbid")

    plane: str | None = Field(
        default=None, description="'Front', 'Top' or 'Right' (or 'xy', 'xz', 'yz')"
    )
    point: list[float] | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="Instead of plane: a point on the mirror plane, mm",
    )
    normal: list[float] | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="With point: the plane's normal, along x, y or z",
    )

    @model_validator(mode="after")
    def _one_plane(self) -> PatternMirror:
        """A named plane, or a point and a normal."""
        if (self.plane is None) == (self.point is None or self.normal is None):
            raise ValueError("give plane, or point and normal")
        return self


class PatternMembersInput(CompatInput):
    """Input schema for copying weldment bodies instead of drawing them again.

    Attributes:
        members (list[str]): What to copy.
        along (PatternStep | None): Copies in a row.
        along2 (PatternStep | None): A second row direction: a grid.
        around (PatternAround | None): Copies around an axis.
        mirror (PatternMirror | None): A mirrored copy.
        skip (list | None): Linear instances to leave out.
        name (str | None): The pattern feature's name.
        folder (str | None): The feature-tree folder.
    """

    members: list[str] = Field(
        min_length=1,
        description="What to copy: member features (every body), body names, plates "
        "('BP-1 Base plate'), end-cap features; finish the original first (trims, "
        "plates, cuts, caps): they are copied as they are",
    )
    along: PatternStep | None = Field(
        default=None, description="Copies in a row: direction, spacing, count"
    )
    along2: PatternStep | None = Field(
        default=None, description="With along: a second direction, for a grid"
    )
    around: PatternAround | None = Field(
        default=None, description="Copies around an axis: axis, point, count, angle"
    )
    mirror: PatternMirror | None = Field(
        default=None, description="A mirrored copy: plane, or point and normal"
    )
    skip: list[int | list[int]] | None = Field(
        default=None,
        description="Instances of a row to leave out, 1-based (the original is 1): "
        "[4, 5]; on a grid [[2, 3]] (along, along2)",
    )
    name: str | None = Field(
        default=None, description="The pattern's name, e.g. 'Frames pattern'"
    )
    folder: str | None = Field(
        default=None,
        description="Feature-tree folder; default the folder holding the first member",
    )

    @model_validator(mode="after")
    def _one_pattern(self) -> PatternMembersInput:
        """One of along, around or mirror; along2 and skip with along only."""
        if sum(x is not None for x in (self.along, self.around, self.mirror)) != 1:
            raise ValueError("give along, around or mirror, one of them")
        if self.along2 is not None and self.along is None:
            raise ValueError("along2 needs along")
        if self.skip:
            if self.along is None:
                raise ValueError("skip works with along only")
            counts = [self.along.count] + ([self.along2.count] if self.along2 else [])
            for item in self.skip:
                position = item if isinstance(item, list) else [item]
                if len(position) != len(counts):
                    raise ValueError(
                        f"skip entry {item} needs {len(counts)} number(s), one per "
                        "direction"
                    )
                if all(p == 1 for p in position):
                    raise ValueError("the original (instance 1) can't be skipped")
                if any(p < 1 or p > c for p, c in zip(position, counts, strict=True)):
                    raise ValueError(f"skip entry {item} is outside the pattern")
        return self


class CreateVentHolesInput(CompatInput):
    """Input schema for galvanizing vent and drain holes.

    Attributes:
        members (list[str]): Member feature or body names.
        diameter (float): Hole diameter, mm.
        from_end_mm (float): Hole centre from the member's end, mm.
        hang (list[float]): Up while the piece hangs in the bath.
        folder (str | None): The feature-tree folder.
        define (str): How the hole sketches are tied down.
    """

    members: list[str] = Field(
        min_length=1,
        description="Hollow members: structural member feature names (every body "
        "of them) or body names, as create_trim_extend takes them",
    )
    diameter: float = Field(
        gt=0,
        le=100,
        description="Hole diameter, mm: the galvanizer's table; never under 10 mm",
    )
    from_end_mm: float = Field(
        default=30.0,
        ge=5.0,
        description="Hole centre from the member's end, mm: as near the joint as "
        "the weld allows",
    )
    hang: list[float] = Field(
        default_factory=lambda: [0.0, 1.0, 0.0],
        min_length=3,
        max_length=3,
        description="Which way is up while the piece hangs in the zinc bath; "
        "the vent goes near the higher end, the drain near the lower",
    )
    folder: str | None = Field(
        default="Vent holes",
        description="Feature-tree folder for the holes (made if missing); null for none",
    )
    define: Literal["dimensions", "none"] = Field(
        default="dimensions",
        description="'dimensions' dimensions each hole's sketch from the origin "
        "(fully defined); 'none' leaves it under defined",
    )


class ExportPlateDxfsInput(CompatInput):
    """Input schema for the plates' DXFs.

    Attributes:
        output_dir (str): Where the DXFs and the list go.
        prefix (str | None): Put before every file name.
    """

    output_dir: str = Field(
        min_length=1, description="Folder for the DXFs and plates.csv (made if missing)"
    )
    prefix: str | None = Field(
        default=None,
        description="Put before every file name, e.g. the job number 'OP-2026-014_'",
    )


class GetCutListInput(CompatInput):
    """Input schema for reading the cut list.

    Attributes:
        update (bool): Update the cut list first.
        export_path (str | None): Also write it to this CSV file.
    """

    update: bool = Field(
        default=True, description="Update the cut list before reading it"
    )
    export_path: str | None = Field(
        default=None, description="Also write the cut list to this .csv file"
    )


async def register_weldment_tools(
    mcp: FastMCP, adapter: SolidWorksAdapter, config: Any
) -> int:
    """Register the weldment tools.

    Args:
        mcp (FastMCP): The server.
        adapter (SolidWorksAdapter): The SolidWorks adapter.
        config (Any): Configuration values for the operation.

    Returns:
        int: How many tools were registered.
    """
    mcp = with_tool_boundary(mcp)

    @mcp.tool()
    async def list_weldment_profiles(
        input_data: ListWeldmentProfilesInput | None = None,
    ) -> dict[str, Any]:
        """List SolidWorks' weldment profile library: standards, profile types and sizes, or search it.

        Use it before `create_structural_members` to find the exact size name: the
        library names sizes its own way (DIN 'HE400B' for HEB400, BSI 'RHS250x250x12.5'
        for SHS 250x250x12.5), so search with `query`. No input lists the standards
        and their types; `standard` lists its types with their sizes;
        `standard` and `profile_type` list one type's sizes. Needs: SolidWorks
        running. Read-only. Returns: `profiles` with `roots` (the library folders)
        and `standards`, `types`, `sizes` or `matches` (`standard`, `type`, `size`,
        `path`).

        Args:
            input_data: Optional `standard`, `profile_type`, `query` and `limit`.

        Returns:
            dict[str, Any]: The library listing or the search matches.
        """
        input_data = normalize_input(input_data, ListWeldmentProfilesInput)
        require_capability(
            adapter, "list_weldment_profiles", "Active adapter has no weldment library"
        )
        result = await adapter.list_weldment_profiles(
            input_data.standard,
            input_data.profile_type,
            input_data.query,
            input_data.limit,
        )
        require_success(result, context="Failed to read the weldment profiles")
        data = result.data or {}
        if "matches" in data:
            message = (
                f"{len(data['matches'])} profile size(s) match {input_data.query!r}"
            )
        elif "sizes" in data:
            message = f"{len(data['sizes'])} size(s) of {data.get('type')}"
        elif "types" in data:
            message = f"{len(data['types'])} profile type(s) in {data.get('standard')}"
        else:
            message = f"{len(data.get('standards', []))} weldment profile standard(s)"
        return {
            "status": "success",
            "message": message,
            "profiles": data,
            "execution_time": result.execution_time,
        }

    @mcp.tool()
    async def create_structural_members(
        input_data: CreateStructuralMembersInput,
    ) -> dict[str, Any]:
        """Put a weldment profile (IPE, HEB, SHS, RHS, UPN, angle...) on many segments in one call: columns, beams, truss chords and web members.

        Each segment ([x1, y1, z1, x2, y2, z2] mm, centre lines, y up) becomes
        a member with its cut-list length and end angles; or give a `sketch`.
        Segments joined end to end are mitred, except where a column already
        stands; one call is one profile. Each profile's height points `up`
        (default model y: nothing sloped comes out rolled); columns need an
        `up` across them. Draw one of a repeated member and copy it with
        `pattern_members`. Then `create_trim_extend` where members meet;
        sizes from `list_weldment_profiles`; many segments go in a
        `segments_file`. The new sketch is fully defined (`define`): nodes
        dimensioned to the main planes. Give `name` (the sketch becomes
        '<name> skeleton') and `folder`; draw members before plates. Needs:
        an open part. Changes: adds a weldment feature (first time), a 3D
        sketch and the members. Returns: `members` with `feature`, `profile`,
        `sketch`, `segments`, `groups`, `bodies_after`, `orientation`
        (measured: `max_roll_deg`), `skeleton` and `folder`; a `warning` when
        a member couldn't face `up`.

        Args:
            input_data: `size` plus `standard` and `profile_type` (or
                `profile_path`), and `segments`, `segments_file` or `sketch`;
                corner, rotation and grouping options have defaults.

        Returns:
            dict[str, Any]: The new structural member feature.
        """
        input_data = normalize_input(input_data, CreateStructuralMembersInput)
        require_capability(
            adapter, "create_structural_members", "Active adapter can't make weldments"
        )
        params = input_data.model_dump(exclude_none=True)
        if input_data.segments_file:
            params["segments"] = load_segments(input_data.segments_file)
            params.pop("segments_file", None)
        result = await adapter.create_structural_members(params)
        require_success(result, context="Failed to create the structural members")
        data = result.data or {}
        profile = data.get("profile") or {}
        reply: dict[str, Any] = {
            "status": "success",
            "message": f"Added {data.get('segments')} {profile.get('size')} member(s) "
            f"as {data.get('feature')}",
            "members": data,
            "execution_time": result.execution_time,
        }
        warning = (data.get("orientation") or {}).get("warning")
        if warning:
            reply["warning"] = warning
        return reply

    @mcp.tool()
    async def create_trim_extend(
        input_data: CreateTrimExtendInput,
    ) -> dict[str, Any]:
        """Trim or extend structural members where they meet: beams to columns, joists coped to beams, braces to the frame (Trim/Extend).

        Use it after `create_structural_members` wherever members meet: members
        drawn centre line to centre line run into each other until trimmed, and
        the cut list then gives centre-line lengths, not cut lengths.
        `end_trim` cuts every member at the `boundary` members; `cut` 'coped'
        notches them to the boundary's shape (joists with tops flush with the
        beams they frame into), 'simple' cuts square. 'miter', 'butt1' and
        'butt2' join one member to one other at a corner. `weld_gap_mm` leaves
        room for the weld. Names are member features (all their bodies) or
        bodies (`Joists IPE160[3]`). Needs: an open weldment part. Changes: adds
        a Trim/Extend feature. Returns: `trim` with `feature`, `members` (each
        body's `name`, `now`, its name after the trim, which SolidWorks changes,
        `volume_change_mm3` and `changed`), `boundary`, `unchanged` (members it didn't
        reach, also named in a `warning`) and `bodies_after`; `get_cut_list`
        then gives the new lengths. Member feature names keep working after.

        Args:
            input_data: `members` and `boundary` (names); `corner_type`
                (end_trim), `cut` (simple), `weld_gap_mm` (0),
                `allow_extension` (true) and `name` have defaults.

        Returns:
            dict[str, Any]: The Trim/Extend feature and what it changed.
        """
        input_data = normalize_input(input_data, CreateTrimExtendInput)
        require_capability(
            adapter, "create_trim_extend", "Active adapter can't trim members"
        )
        result = await adapter.create_trim_extend(
            input_data.model_dump(exclude_none=True)
        )
        require_success(result, context="Failed to trim the structural members")
        data = dict(result.data or {})
        warning = data.pop("warning", None)
        reply: dict[str, Any] = {
            "status": "success",
            "message": f"Trimmed {len(data.get('members', []))} member body(ies) to "
            f"{len(data.get('boundary', []))} boundary body(ies) as "
            f"{data.get('feature')}",
            "trim": data,
            "execution_time": result.execution_time,
        }
        if warning:
            reply["warning"] = warning
        return reply

    @mcp.tool()
    async def check_joints(
        input_data: CheckJointsInput | None = None,
    ) -> dict[str, Any]:
        """Check a weldment's joints: members that run into each other, member ends left open, bodies touching nothing.

        Use it after trimming, before calling a frame done: centre lines drawn
        node to node overlap until `create_trim_extend` cuts them, and a
        picture doesn't show a brace buried in a chord. `overlaps` lists each
        pair sharing volume (mm3, and where); `open_ends` the member ends that
        touch nothing (cap them with `create_end_caps`, or say why they stay
        open); `partly_open_ends` ends that hang off what they meet; `loose`
        the bodies touching nothing; `groups` how many separate pieces there
        are. `clean` means no overlap, no loose body and no end half off. It
        takes about 0.2 s a body: give `members` to check one group. Needs: an
        open weldment part. Read-only. Returns: `joints` with `bodies`,
        `checked_bodies`, `members`, `overlaps` (`count`, `volume_mm3`,
        `pairs`), `open_ends`, `partly_open_ends`, `loose`, `loose_count`,
        `groups`, `group_sizes`, `clean`, `pairs_checked` and `seconds`.

        Args:
            input_data: Optional `members`, `overlap_mm3` (1), `contact_mm`
                (0.5) and `max_listed` (25).

        Returns:
            dict[str, Any]: The joint check.
        """
        input_data = normalize_input(input_data, CheckJointsInput)
        require_capability(adapter, "check_joints", "Active adapter can't check joints")
        result = await adapter.check_joints(input_data.model_dump(exclude_none=True))
        require_success(result, context="Failed to check the joints")
        data = result.data or {}
        overlaps = data.get("overlaps", {}).get("count", 0)
        open_ends = data.get("open_ends", {}).get("count", 0)
        return {
            "status": "success",
            "message": f"{overlaps} overlapping pair(s), {open_ends} open end(s), "
            f"{data.get('loose_count', 0)} loose body(ies) in {data.get('checked_bodies')} "
            f"of {data.get('bodies')} bodies"
            + ("; clean" if data.get("clean") else ""),
            "joints": data,
            "execution_time": result.execution_time,
        }

    @mcp.tool()
    async def create_end_caps(input_data: CreateEndCapsInput) -> dict[str, Any]:
        """Close the open ends of hollow structural members with end caps (End Cap).

        Use it on the ends `check_joints` lists as open, before the frame goes
        to the shop: an open tube fills with water, and a sealed one bursts in
        the galvanizing bath (so galvanized work also needs vent holes). Give
        `ends` (a point at each end, mm) or `open_ends: true` for every open
        end. `placement` 'inward' keeps the overall size (the member gets
        shorter by the cap); 'outward' adds the cap's thickness; 'internal'
        sets it `depth_mm` inside. One feature caps them all, and each cap is a
        cut-list plate. Needs: an open weldment part. Changes: adds an End Cap
        feature and one body per cap. Returns: `caps` with `feature`, `caps`
        (the count), `ends` (`body`, `at_mm`), `placement`, `thickness_mm`,
        `inset_mm` and `bodies_after`.

        Args:
            input_data: `ends` or `open_ends`; `thickness_mm` (5),
                `placement` (inward), `inset_mm` (0), `depth_mm`,
                `contact_mm` and `name` have defaults.

        Returns:
            dict[str, Any]: The End Cap feature and the ends it capped.
        """
        input_data = normalize_input(input_data, CreateEndCapsInput)
        require_capability(
            adapter, "create_end_caps", "Active adapter can't add end caps"
        )
        result = await adapter.create_end_caps(input_data.model_dump(exclude_none=True))
        require_success(result, context="Failed to add the end caps")
        data = dict(result.data or {})
        warning = data.pop("warning", None)
        reply: dict[str, Any] = {
            "status": "success",
            "message": f"Capped {data.get('caps')} end(s) as {data.get('feature')}",
            "caps": data,
            "execution_time": result.execution_time,
        }
        if warning:
            reply["warning"] = warning
        return reply

    @mcp.tool()
    async def create_plates(input_data: CreatePlatesInput) -> dict[str, Any]:
        """Add plates to a weldment in one call: base, splice, cap, gusset and connection plates, with their bolt holes.

        Use it for every plate of a frame or truss instead of planes, sketches and
        extrusions one by one. Each plate is an `outline` (or `width` x `height`)
        and `holes` in its own x/y, placed by `origin`, `normal` (the thickness
        direction) and optionally `x_axis`. Each becomes its own body (never
        merged into the members), named '<mark> <name>', described in the cut
        list ('PL15 250x250', with MARK and THICKNESS), on a sketch dimensioned
        from the origin (fully defined) and a plane tied to the main planes
        (plates on one level share it), all in `folder`. Many plates: write them
        to a JSON file and give `plates_file`. Draw the members first. Needs: an
        open part, no sketch open. Changes: adds planes, sketches and one
        extrusion per plate. Returns: `plates` (each `name`, `feature`, `sketch`,
        `plane`, `body`, `description`, `thickness_mm`, `holes`,
        `definition`), `planes_made`, `folder` and `bodies_after`.

        Args:
            input_data: `plates` (or `plates_file`); `folder` ('Plates') and
                `define` ('dimensions') have defaults.

        Returns:
            dict[str, Any]: The plates made.
        """
        input_data = normalize_input(input_data, CreatePlatesInput)
        require_capability(adapter, "create_plates", "Active adapter can't add plates")
        params = input_data.model_dump(exclude={"plates_file"})
        if input_data.plates_file:
            params["plates"] = load_plates(input_data.plates_file)
        result = await adapter.create_plates(params)
        require_success(result, context="Failed to add the plates")
        data = result.data or {}
        plates = data.get("plates") or []
        loose = [
            p["name"]
            for p in plates
            if p.get("definition") not in ("fully defined", "not checked")
        ]
        reply: dict[str, Any] = {
            "status": "success",
            "message": f"Added {len(plates)} plate(s), "
            f"{sum(p.get('holes', 0) for p in plates)} hole(s)",
            "plates": data,
            "execution_time": result.execution_time,
        }
        if loose:
            reply["warning"] = f"Not fully defined: {', '.join(loose[:10])}"
        return reply

    @mcp.tool()
    async def create_cuts(input_data: CreateCutsInput) -> dict[str, Any]:
        """Cut bolt holes, copes and notches in weldment members in one call.

        Use it for the holes a bolted joint needs in a beam's web or flange
        and for the copes at beam ends, instead of sketches and cuts one by
        one; a plate's own holes go in `create_plates`. Each cut is an
        `outline` (or `width` x `height`), `holes`, or both, in its own x/y,
        placed by `origin` (on the middle of the steel it cuts), `normal` (the
        way it goes through) and optionally `x_axis`, and reaching `depth`
        mm, half each side: through a web and the fin plate on it, short of
        the next member. Each sketch is dimensioned from the origin (fully
        defined) on a plane tied to the main planes, the cut is named, and
        `bodies` lists what it went through. Many cuts: give `cuts_file`.
        Needs: an open part with the members in, no sketch open. Changes:
        adds planes, sketches and one cut per entry, in `folder`. Returns:
        `cuts` with `cuts` (each `name`, `feature`, `sketch`, `plane`,
        `holes`, `bodies`, `definition`), `planes_made` and `folder`.

        Args:
            input_data: `cuts` (or `cuts_file`); `folder` ('Cuts') and
                `define` ('dimensions') have defaults.

        Returns:
            dict[str, Any]: The cuts made.
        """
        input_data = normalize_input(input_data, CreateCutsInput)
        require_capability(adapter, "create_cuts", "Active adapter can't cut members")
        params = input_data.model_dump(exclude={"cuts_file"})
        if input_data.cuts_file:
            params["cuts"] = load_cuts(input_data.cuts_file)
        result = await adapter.create_cuts(params)
        require_success(result, context="Failed to cut the members")
        data = result.data or {}
        cuts = data.get("cuts") or []
        loose = [
            c["name"]
            for c in cuts
            if c.get("definition") not in ("fully defined", "not checked")
        ]
        bodies = {b for c in cuts for b in c.get("bodies") or []}
        reply: dict[str, Any] = {
            "status": "success",
            "message": f"Made {len(cuts)} cut(s), "
            f"{sum(c.get('holes', 0) for c in cuts)} hole(s)"
            + (f", through {len(bodies)} body(ies)" if bodies else ""),
            "cuts": data,
            "execution_time": result.execution_time,
        }
        if loose:
            reply["warning"] = f"Not fully defined: {', '.join(loose[:10])}"
        return reply

    @mcp.tool()
    async def pattern_members(input_data: PatternMembersInput) -> dict[str, Any]:
        """Copy finished members, plates or caps in a row, a grid, around an axis, or mirrored, instead of drawing each.

        Use it whenever an element repeats inside one piece: balusters, frames
        on a pitch, purlins per bay, stiffeners, base plates under a row of
        columns, the other hand of a symmetric frame. Build and finish one
        original (trims, plates, cuts, caps), then copy it with `along`
        (`direction`, `spacing`, `count`; `along2` for a grid; `skip` leaves
        instances out, e.g. at a door), `around` (`axis`, `point`, `count`,
        `angle`) or `mirror` (`plane`, or `point` and `normal`). The copies are
        one SolidWorks pattern feature on a named main axis or a fully defined
        direction line: a specialist changes the pitch or count in one place,
        and the cut list counts the copies with their original. Identical
        separate pieces are better one part placed N times in an assembly
        (`add_components`). Needs: an open weldment part with the original
        built, no sketch open. Changes: adds the pattern (and its axis or
        direction line) in `folder`. Returns: `pattern` with `feature`,
        `kind`, `instances`, `seed_bodies`, `bodies_added`, `bodies_after`,
        `references` and `folder`; a `warning` when the body count is off.

        Args:
            input_data: `members` and one of `along` (+ `along2`, `skip`),
                `around` or `mirror`; `name` and `folder` optional.

        Returns:
            dict[str, Any]: The pattern made.
        """
        input_data = normalize_input(input_data, PatternMembersInput)
        require_capability(
            adapter, "pattern_members", "Active adapter can't pattern bodies"
        )
        result = await adapter.pattern_members(input_data.model_dump(exclude_none=True))
        require_success(result, context="Failed to copy the members")
        data = dict(result.data or {})
        warning = data.pop("warning", None)
        reply: dict[str, Any] = {
            "status": "success",
            "message": f"{data.get('kind')} pattern {data.get('feature')}: "
            f"{data.get('instances')} instance(s), {data.get('bodies_added')} "
            "body(ies) added",
            "pattern": data,
            "execution_time": result.execution_time,
        }
        if warning:
            reply["warning"] = warning
        return reply

    @mcp.tool()
    async def create_vent_holes(input_data: CreateVentHolesInput) -> dict[str, Any]:
        """Cut the vent and drain holes hot-dip galvanizing needs in closed hollow members (RHS, SHS).

        Use it on every member whose inside the zinc can't reach otherwise,
        before the cut list and drawings: a sealed tube bursts in the bath.
        For each member it finds the walls from the body itself, then cuts a
        vent near the end that hangs higher, through the wall facing up, and
        a drain near the lower end, through the wall facing down (diagonally
        opposite, as EN ISO 14713-2 asks). Each hole goes through the near
        wall only. Size and number come from the galvanizer; `hang` is up in
        the bath. Round tubes are skipped (no flat wall). Needs: an open
        weldment part, no sketch open. Changes: one cut per hole, in `folder`.
        Returns: `vents` with `holes` (each `member`, `kind`, `feature`,
        `at_mm`), `skipped` and `folder`.

        Args:
            input_data: `members` and `diameter`; `from_end_mm` (30), `hang`
                (up), `folder` and `define` have defaults.

        Returns:
            dict[str, Any]: The holes cut and the members skipped.
        """
        input_data = normalize_input(input_data, CreateVentHolesInput)
        require_capability(
            adapter, "create_vent_holes", "Active adapter can't cut vent holes"
        )
        result = await adapter.create_vent_holes(input_data.model_dump())
        require_success(result, context="Failed to cut the vent holes")
        data = result.data or {}
        holes = data.get("holes") or []
        reply: dict[str, Any] = {
            "status": "success",
            "message": f"Cut {len(holes)} vent and drain hole(s)",
            "vents": data,
            "execution_time": result.execution_time,
        }
        if data.get("skipped"):
            reply["warning"] = (
                f"{len(data['skipped'])} member(s) skipped; see skipped for why"
            )
        return reply

    @mcp.tool()
    async def export_plate_dxfs(input_data: ExportPlateDxfsInput) -> dict[str, Any]:
        """Write a DXF of every distinct plate in the weldment for the laser or plasma table, with a plates.csv list.

        Use it after the plates are in and the part is saved: each cut-list item
        that is a flat plate (plates, end caps, gussets; not the members) gives
        one DXF of its face at 1:1 with every hole, named
        '<mark or description>_t<thickness>_x<quantity>.dxf', and plates.csv
        lists file, mark, description, thickness, quantity and area. Needs: an
        open weldment part saved to disk. Changes: writes files only. Returns:
        `files` (each `file`, `mark`, `description`, `thickness_mm`, `quantity`,
        `area_mm2`), `csv` and `skipped`.

        Args:
            input_data: `output_dir`, and optionally `prefix`.

        Returns:
            dict[str, Any]: The DXFs written.
        """
        input_data = normalize_input(input_data, ExportPlateDxfsInput)
        require_capability(
            adapter, "export_plate_dxfs", "Active adapter can't export plates"
        )
        result = await adapter.export_plate_dxfs(
            input_data.model_dump(exclude_none=True)
        )
        require_success(result, context="Failed to export the plates")
        data = result.data or {}
        files = data.get("files") or []
        return {
            "status": "success",
            "message": f"Wrote {len(files)} plate DXF(s), "
            f"{sum(f.get('quantity', 0) for f in files)} plate(s) to cut",
            "dxfs": data,
            "execution_time": result.execution_time,
        }

    @mcp.tool()
    async def get_cut_list(input_data: GetCutListInput | None = None) -> dict[str, Any]:
        """Read the active weldment part's cut list: every member size with its quantity, length, end angles and mass, ready for the shop.

        Use it after `create_structural_members` to check the build and to hand
        the cutting list on: items group identical members. `export_path` also
        writes it as CSV. Masses use the part's material (`set_material` first; with
        none, SolidWorks' default density is used and a `warning` says so). Needs:
        an open weldment part. Changes: updates the cut list. Returns: `cut_list`
        with `items` (`item`, `name`, `description`, `quantity`, `length_mm`,
        `angle1_deg`, `angle2_deg`, `total_length_mm`, `profile_total_length_mm`,
        `material`, `mass_each_kg`, `mass_total_kg`), `pieces`, `total_length_mm`
        and `total_mass_kg`.

        Args:
            input_data: Optional `update` (true) and `export_path` (.csv).

        Returns:
            dict[str, Any]: The cut list.
        """
        input_data = normalize_input(input_data, GetCutListInput)
        require_capability(adapter, "get_cut_list", "Active adapter has no cut list")
        result = await adapter.get_cut_list(input_data.update, input_data.export_path)
        require_success(result, context="Failed to read the cut list")
        data = dict(result.data or {})
        warning = data.pop("warning", None)
        reply: dict[str, Any] = {
            "status": "success",
            "message": f"{data.get('item_count', 0)} cut list item(s), "
            f"{data.get('pieces', 0)} piece(s), {data.get('total_mass_kg')} kg",
            "cut_list": data,
            "execution_time": result.execution_time,
        }
        if warning:
            reply["warning"] = warning
        return reply

    return 11
