# Weldments

Steel frames, trusses and platforms as SolidWorks weldments: a profile from
the weldment library (IPE, HEB, SHS, RHS, UPN, angles...) on many sketch
segments in one call, members trimmed where they meet, and a cut list with
every member's length, end angles, quantity and mass. Checked live on
SolidWorks 2025 SP4: a 6-panel Warren truss (chords SHS 100x100x5, web SHS
60x60x4) gave 459.23 kg, the part's own mass, split over five cut-list
items; IPE160 joists coped to IPE240 beams with a 5 mm weld gap came out
1983.8 mm between beams 2000 mm apart.

The order for a frame: `list_weldment_profiles` to find each size,
`create_structural_members` once per profile (columns, beams, joists,
bracing) on centre lines in model axes (y up), `create_trim_extend`
wherever one member frames into another, `set_material`, then
`get_cut_list`.

### `list_weldment_profiles`

List SolidWorks' weldment profile library: standards, profile types and sizes, or search it.

**SolidWorks automation note:** The library folder is Tools > Options > File Locations > Weldment Profiles (`swFileLocationsWeldmentProfiles`). Since SolidWorks 2018 each profile type is one `.sldlfp` with a configuration per size (`ISldWorks.GetConfigurationNames`); an older folder-per-type library is read too. The search matches every word against type and size names, so `HEB400` finds DIN's `HE400B` and `250x250x12` finds BSI's `RHS250x250x12.5`; shop shorthand for an equal angle, `L50x5`, finds `L50x50x5`, and a size named exactly comes first.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `standard` | `str?` | — | `None` | A standard's folder, e.g. `DIN`, `BSI`, `iso`, `ansi` |
| `profile_type` | `str?` | — | `None` | A profile type of that standard, e.g. `ipe`, `hea`, `shs` |
| `query` | `str?` | — | `None` | Search words over every type and size, e.g. `HEB400`, `shs 250x250` |
| `limit` | `int` | — | `50` | The most search results |

**Sample call:**

```json
{"query": "HEB400"}
```

---

### `create_structural_members`

Put a weldment profile on many segments in one call: columns, beams, truss chords and web members.

**Prerequisite:** An open part

**SolidWorks automation note:** The segments are drawn as a 3D sketch (`ISketchManager.CreateLine`) or taken from an existing sketch. `IFeatureManager.InsertWeldmentFeature` comes first in a part that isn't a weldment yet, then `InsertStructuralWeldment5` with the profile file, the size as its configuration and one `IStructuralMemberGroup` per chain of segments joined end to end. SolidWorks refuses a group that branches (a truss's chords and web together); if it refuses the chains, each segment becomes its own group. The profile's `.sldlfp`, which SolidWorks leaves open, is closed again.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `size` | `str` | ✅ | | Profile size as the library names it, e.g. `IPE300`, `HE400B`, `RHS100x100x5` |
| `standard` | `str?` | — | `None` | Library standard, e.g. `DIN` |
| `profile_type` | `str?` | — | `None` | Library profile type, e.g. `ipe`, `hea`, `shs` |
| `profile_path` | `str?` | — | `None` | A `.sldlfp` profile of your own instead of `standard` and `profile_type` |
| `segments` | `list[list[float]]?` | — | `None` | Member axes to draw as a new 3D sketch, each `[x1, y1, z1, x2, y2, z2]` in mm |
| `sketch` | `str?` | — | `None` | An existing sketch whose lines and arcs to use instead |
| `segment_names` | `list[str]?` | — | `None` | Only these segments of `sketch`, e.g. `["Line1", "Line4"]` |
| `corner_treatment` | `str` | — | `miter` | Where segments of one chain meet: `miter`, `butt1`, `butt2` or `none` |
| `rotation` | `float` | — | `0.0` | Turn the profile about the member's axis, degrees |
| `mirror_profile` | `str` | — | `none` | Mirror the profile about its `horizontal` or `vertical` axis |
| `merge_arcs` | `bool` | — | `false` | Make tangent arc segments one body |
| `grouping` | `str` | — | `auto` | `auto` groups segments joined end to end; `single` one group; `each` one per segment |
| `name` | `str?` | — | `None` | Name for the new feature |
| `sketch_name` | `str?` | — | `None` | Name for the new 3D sketch drawn from `segments` |

**Sample call:**

```json
{"standard": "DIN", "profile_type": "hea", "size": "HE400B", "segments": [[0, 0, 0, 0, 12000, 0], [9000, 0, 0, 9000, 12000, 0]], "name": "Columns"}
```

---

### `create_trim_extend`

Trim or extend structural members where they meet: beams to columns, joists coped to beams, braces to the frame (Trim/Extend).

**Prerequisite:** An open weldment part

**SolidWorks automation note:** `IFeatureManager.InsertWeldmentTrimFeature2(corner, options, gap, bodies, boundary)`, the bodies as `VT_DISPATCH` arrays; corner `swEndConditionTrim` (4) for an end trim or `Miter`/`Butt1`/`Butt2` (1-3) for a corner of two members, and the options `swWeldmentTrimExtendOptionType_e` flags (allow extension 1 and 2, coped cut 4, weld gap 8). A name is a member feature (every body whose faces it made) or a body. SolidWorks renames the bodies it trims after the feature (`Joists[1]` becomes `Trim/Extend1[1]`), so each member is followed by its centre of mass and its new name returned; a member whose volume and centre of mass didn't move is reported as `unchanged` (a mitre keeps the volume but moves the centre).

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `members` | `list[str]` | ✅ | | Members to trim or extend: member feature names or body names, e.g. `["Joists IPE160"]` |
| `boundary` | `list[str]` | ✅ | | What they are trimmed to, e.g. `["Main beams IPE240"]` |
| `corner_type` | `str` | — | `end_trim` | `end_trim` cuts every member at the boundary; `miter`, `butt1`, `butt2` join one member to one other |
| `cut` | `str` | — | `simple` | `simple` cuts square; `coped` cuts to the boundary's shape (joists with tops flush with the beams) |
| `weld_gap_mm` | `float` | — | `0.0` | Gap left for the weld at each cut, mm |
| `allow_extension` | `bool` | — | `true` | Extend members that stop short of the boundary |
| `name` | `str?` | — | `None` | Name for the new feature |

**Sample call:**

```json
{"members": ["Joists IPE160"], "boundary": ["Main beams IPE240"], "cut": "coped", "weld_gap_mm": 5}
```

---

### `get_cut_list`

Read the active weldment part's cut list: every member size with its quantity, length, end angles and mass.

**Prerequisite:** An open weldment part

**SolidWorks automation note:** The cut-list folders under Solid Bodies (`IBodyFolder.UpdateCutList` first). Each folder's resolved `LENGTH`, `ANGLE1`, `ANGLE2` and `QUANTITY` come from `ICustomPropertyManager.Get6` with byref out-parameters, converted from the document's length unit to mm; its mass from `IMassProperty.AddBodies` on the folder's bodies. SolidWorks' own `TOTAL LENGTH` adds up every item of one profile, so it is reported as `profile_total_length_mm` and the item's own total is its length times its quantity.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `update` | `bool` | — | `true` | Update the cut list before reading it |
| `export_path` | `str?` | — | `None` | Also write the cut list to this `.csv` file |

**Sample call:**

```json
{"export_path": "C:\\steel\\roof-truss-cutlist.csv"}
```
