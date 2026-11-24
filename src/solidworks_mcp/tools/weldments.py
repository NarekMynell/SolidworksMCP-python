"""Weldment tools: structural members, the profile library and the cut list.

What a steel fabricator builds a frame, truss or platform with: one call puts
a profile (IPE, HEB, SHS, RHS, UPN, angle...) from SolidWorks' weldment
library on many sketch segments, and the cut list gives every member's
length, end angles, quantity and mass for the shop.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from fastmcp import FastMCP
from pydantic import Field, model_validator

from ..adapters.base import SolidWorksAdapter
from .input_compat import CompatInput
from .tool_boundary import (
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


class CreateStructuralMembersInput(CompatInput):
    """Input schema for a structural member feature.

    Attributes:
        size (str): The profile size, e.g. ``IPE300``.
        standard (str | None): The library standard, e.g. ``DIN``.
        profile_type (str | None): The library type, e.g. ``ipe``.
        profile_path (str | None): A ``.sldlfp`` of your own instead.
        segments (list[list[float]] | None): Lines to draw, mm.
        sketch (str | None): An existing sketch to use instead.
        segment_names (list[str] | None): Only these segments of it.
        corner_treatment (str): Where segments of one chain meet.
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
        max_length=2000,
        description="Member axes to draw as a new 3D sketch, each "
        "[x1, y1, z1, x2, y2, z2] in mm; draw a continuous member as one segment",
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
    rotation: float = Field(
        default=0.0, description="Turn the profile about the member's axis, degrees"
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
        default=None, description="Name for the new 3D sketch drawn from segments"
    )

    @model_validator(mode="after")
    def _one_profile_one_path(self) -> CreateStructuralMembersInput:
        """A profile from the library or a file, on segments or a sketch."""
        if not self.profile_path and not (self.standard and self.profile_type):
            raise ValueError("give standard and profile_type, or profile_path")
        if (self.segments is None) == (self.sketch is None):
            raise ValueError("give segments or sketch, one of them")
        for index, segment in enumerate(self.segments or []):
            if len(segment) != 6:
                raise ValueError(
                    f"segment {index} has {len(segment)} numbers; give x1, y1, z1, "
                    "x2, y2, z2"
                )
            if math.dist(segment[:3], segment[3:]) < 0.001:
                raise ValueError(f"segment {index} has no length")
        if self.segment_names and not self.sketch:
            raise ValueError("segment_names needs sketch")
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
    name: str | None = Field(default=None, description="Name for the new feature")

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

        Use it for steel frames and trusses instead of sketching profiles and
        extruding them: each segment becomes a member body, with its cut list
        length and end angles. Give `segments` ([x1, y1, z1, x2, y2, z2] mm each,
        drawn as a new 3D sketch) or an existing `sketch`; draw a continuous member
        as one segment. Segments joined end to end are mitred (`corner_treatment`);
        each call is one profile, so a truss is one call for the chords and one for
        the web. Segments are member centre lines in model axes (y up); where one
        member frames into another, finish with `create_trim_extend`. Find the
        size with `list_weldment_profiles`. Needs: an open part.
        Changes: adds a weldment feature (first time), a 3D sketch and the members.
        Returns: `members` with `feature`, `profile`, `sketch`, `segments`, `groups`,
        `grouping` and `bodies_after`.

        Args:
            input_data: `size` plus `standard` and `profile_type` (or
                `profile_path`), and `segments` or `sketch`; corner, rotation and
                grouping options have defaults.

        Returns:
            dict[str, Any]: The new structural member feature.
        """
        input_data = normalize_input(input_data, CreateStructuralMembersInput)
        require_capability(
            adapter, "create_structural_members", "Active adapter can't make weldments"
        )
        result = await adapter.create_structural_members(
            input_data.model_dump(exclude_none=True)
        )
        require_success(result, context="Failed to create the structural members")
        data = result.data or {}
        profile = data.get("profile") or {}
        return {
            "status": "success",
            "message": f"Added {data.get('segments')} {profile.get('size')} member(s) "
            f"as {data.get('feature')}",
            "members": data,
            "execution_time": result.execution_time,
        }

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

    return 4
