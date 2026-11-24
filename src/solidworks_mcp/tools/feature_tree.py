"""Feature-tree tools: folders and names, and an audit of what a CAD specialist sees first.

A part built by many tool calls is a long flat list of ``Sketch12``,
``Plane7`` and ``Boss-Extrude31`` with every sketch under defined; a
specialist opening it can't find anything and anything dragged moves.
``set_feature_folders`` renames features and groups them in folders;
``check_feature_tree`` measures the tree (sketches not fully defined, SolidWorks'
own names, features outside folders) and says what to fix.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..adapters.base import SolidWorksAdapter
from .input_compat import CompatInput
from .tool_boundary import (
    normalize_input,
    require_capability,
    require_success,
    with_tool_boundary,
)


class FolderSpec(BaseModel):
    """One folder and the features that go in it.

    Attributes:
        name (str): The folder's name.
        features (list[str]): Feature names, tree order.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, description="Folder name, e.g. 'Skeleton', 'Bracing'"
    )
    features: list[str] = Field(
        min_length=1,
        description="Feature names to put in it, in tree order (a member's sketch "
        "before the member)",
    )


class SetFeatureFoldersInput(CompatInput):
    """Input schema for renaming features and folding them into folders.

    Attributes:
        rename (dict[str, str] | None): Old name to new name.
        folders (list[FolderSpec] | None): Folders to fill.
    """

    rename: dict[str, str] | None = Field(
        default=None,
        description='Old name to new name, e.g. {"Plane7": "Splice plane 3"}; '
        "applied before the folders",
    )
    folders: list[FolderSpec] | None = Field(
        default=None,
        description="Folders to put features in, each {name, features}; an "
        "existing folder of that name gets them too",
    )

    @model_validator(mode="after")
    def _something(self) -> SetFeatureFoldersInput:
        """At least one change."""
        if not self.rename and not self.folders:
            raise ValueError("give rename or folders")
        return self


class CheckFeatureTreeInput(CompatInput):
    """Input schema for the feature-tree audit.

    Attributes:
        max_listed (int): The most names listed per finding.
    """

    max_listed: int = Field(
        default=20, ge=1, le=200, description="The most names listed per finding"
    )


async def register_feature_tree_tools(
    mcp: FastMCP, adapter: SolidWorksAdapter, config: Any
) -> int:
    """Register the feature-tree tools.

    Args:
        mcp: The server.
        adapter: The adapter the tools call.
        config: The server configuration (unused).

    Returns:
        int: How many tools were registered.
    """
    mcp = with_tool_boundary(mcp)

    @mcp.tool()
    async def set_feature_folders(
        input_data: SetFeatureFoldersInput,
    ) -> dict[str, Any]:
        """Rename features and group them into feature-tree folders, so a CAD specialist can find their way in the model.

        Use it when a part's tree is a long flat list, or `check_feature_tree` reports
        SolidWorks' own names (Sketch12, Plane7) or many features outside
        folders. `rename` goes first; `folders` then puts each listed feature in
        its folder, making the folder when the tree has none of that name and
        moving features up into an existing one. List a member's sketch before
        the member, and a plate's extrusion rather than its sketch (the sketch
        rides inside it). Needs: an open part or assembly. Changes: feature
        names and folders only. Returns: `tree` with `renamed`,
        `unknown_names`, `names_refused` and `folders` (each `folder`,
        `moved`, `missing`, `refused`).

        Args:
            input_data: `rename` and/or `folders`.

        Returns:
            dict[str, Any]: What was renamed and moved.
        """
        input_data = normalize_input(input_data, SetFeatureFoldersInput)
        require_capability(
            adapter, "set_feature_folders", "Active adapter can't organize the tree"
        )
        result = await adapter.set_feature_folders(
            input_data.model_dump(exclude_none=True)
        )
        require_success(result, context="Failed to organize the feature tree")
        data = result.data or {}
        moved = sum(len(f.get("moved") or []) for f in data.get("folders") or [])
        problems = (
            sum(
                len(f.get("missing") or []) + len(f.get("refused") or [])
                for f in data.get("folders") or []
            )
            + len(data.get("unknown_names") or [])
            + len(data.get("names_refused") or [])
        )
        reply: dict[str, Any] = {
            "status": "success",
            "message": f"Renamed {len(data.get('renamed') or {})} feature(s), "
            f"moved {moved} into folders",
            "tree": data,
            "execution_time": result.execution_time,
        }
        if problems:
            reply["warning"] = (
                f"{problems} name(s) missing or refused; see unknown_names, "
                "names_refused and each folder's missing and refused"
            )
        return reply

    @mcp.tool()
    async def check_feature_tree(
        input_data: CheckFeatureTreeInput | None = None,
    ) -> dict[str, Any]:
        """Audit the active part's feature tree as a CAD specialist would: sketches not fully defined, SolidWorks' own names, features outside folders.

        Use it before handing a model over, and at the end of every build: a
        sketch that isn't fully defined moves when dragged, ``Sketch12`` and
        ``Plane7`` say nothing, and a long tree without folders can't be read.
        `verdict` is 'clean' when there is nothing to fix; `issues` says what
        to fix, and `not_fully_defined` and `generic_names` name the features
        (fix with `set_feature_folders`). Needs: an open part. Read-only.
        Returns: `audit` with `features`, `by_type`, `folders`,
        `outside_folders`, `generic_names`, `generic_count`, `sketches`,
        `not_fully_defined`, `suppressed`, `issues` and `verdict`.

        Args:
            input_data: Optional `max_listed` (20).

        Returns:
            dict[str, Any]: The audit.
        """
        input_data = normalize_input(input_data, CheckFeatureTreeInput)
        require_capability(
            adapter, "check_feature_tree", "Active adapter can't audit a model"
        )
        result = await adapter.check_feature_tree(input_data.model_dump())
        require_success(result, context="Failed to audit the model")
        data = result.data or {}
        return {
            "status": "success",
            "message": f"{data.get('verdict')}: {len(data.get('issues') or [])} "
            f"issue(s) in {data.get('features')} feature(s)",
            "audit": data,
            "execution_time": result.execution_time,
        }

    return 2
