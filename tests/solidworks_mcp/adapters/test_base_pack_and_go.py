"""Pack and Go on the base adapter: not implemented there, and it says so."""

from __future__ import annotations

import pytest

from .test_base_part_features import _bare


@pytest.mark.asyncio
async def test_pack_and_go_names_itself_as_missing() -> None:
    result = await _bare().pack_and_go_assembly(
        source_path="C:/a/frame.sldasm", target_dir="C:/b"
    )

    assert result.error == "pack_and_go_assembly is not implemented by this adapter"
