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
wherever one member frames into another, `check_joints` until no member
runs into another, `create_end_caps` on the open hollow ends,
`create_plates` for base, splice and connection plates with their bolt
holes, `create_cuts` for the bolt holes and copes in the members,
`create_vent_holes` in closed hollow members of galvanized work, `set_material`, then `get_cut_list`, `export_plate_dxfs` for the
cutting table and `check_feature_tree` before the model is handed over.
Sections the library doesn't have, a materials catalog's own, become
profiles with `build_weldment_profiles`.

Every sketch these tools draw comes out fully defined: a skeleton's nodes
are dimensioned to the Right, Top and Front planes and a plate's corners and
holes from the origin, so nothing moves when dragged and a specialist can
edit any coordinate. A profile's sketch is the exception where SolidWorks'
Fully Define Sketch can't manage it (an I-beam of 11 checked):
`build_weldment_profiles` names those. Give each call a `name` and a `folder` to keep the tree
readable.

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

### `build_weldment_profiles`

Build weldment profiles (`.sldlfp`) from exact section outlines, many in one call: a materials catalog as a SolidWorks profile library.

**Prerequisite:** SolidWorks running

**SolidWorks automation note:** Each profile is a new part (`ISldWorks.NewPart`, the default part template) with one sketch on the Front plane (found by its place in the tree, in any language): the outline's lines (`ISketchManager.CreateLine`) and true arcs (`Create3PointArc` through the arc's ends and the middle of its sweep), a point (`CreatePoint`) at each pierce point, all with `AddToDB` on, then, with `define: dimensions`, SolidWorks' Fully Define Sketch and a tie to the origin. `ISketch.CheckFeatureUse` for a sweep section must find no fault and one closed contour per loop. The custom properties go in at file level (`ICustomPropertyManager.Add3`), the sketch is selected and `IModelDocExtension.SaveAs2` saves the part, silently, as a library feature part in a private folder. The part is closed (`ISldWorks.CloseDoc`, by its full path once saved) on every way out, a failure and an interrupt too, and only a profile that was saved, closed and checked is copied into the library, under the spelling asked for. Each profile is its own SolidWorks operation: a dialog fails that profile alone, one left open 10 s is refused (Cancel, No, Abort, its only button, or closed), and a cancelled call stops between profiles. The four settings Fully Define Sketch switches off are put back when the call ends. What SolidWorks 2025 took and refused on the way: [COM pitfall #48](../../agents/com-api-pitfalls.md#48-weldment-profiles-what-solidworks-2025-takes-and-what-it-refuses).

Checked live on 2025 SP4 (receipt: `docs/proofs/weldment-profiles.json`): the 11 profiles of a materials catalog (rectangular, round and oval tubes, an angle, a channel, an I-beam, a tee, a flat, and round, square and hex bars), each then a structural member 1000 mm long whose volume over 1000 was the catalog's area within 0.0001 % and whose cut-list item carried the profile's properties. Fully Define Sketch left the I-beam under defined. Each profile waits twice for `SOLIDWORKS_MCP_DOC_SETTLE_SECONDS` (default 1 s), after its new part and after it is closed; at the live test's 2.5 s a profile took 8.3 to 12.8 s with `define: dimensions` (median 8.7 s) and 7.1 to 7.7 s with `none`. Each profile's `seconds` is in the reply; a long spec can be built in parts with `only`.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `spec_file` | `str` | ✅ | | A `.json` file of profiles, `{"profiles": [...]}` (below) |
| `root` | `str?` | — | `None` | The library folder the paths go under; default the first Weldment Profiles folder of SolidWorks' File Locations |
| `overwrite` | `bool` | — | `false` | Build profiles whose file exists again; `false` skips them |
| `only` | `list[str]?` | — | `None` | Only these paths of the spec, e.g. `["Opus/angles/L63x40x5.sldlfp"]` |
| `define` | `str` | — | `dimensions` | `dimensions` runs Fully Define Sketch on each sketch (some outlines stay under defined: each profile's `definition` says); `none` leaves it as drawn, under defined |

**Sample call:**

```json
{"spec_file": "C:\\catalog\\opus_profiles.json", "root": "D:\\Library\\Weldment Profiles", "only": ["Opus/rhs-welded/80x60x2.7.sldlfp"]}
```

#### The spec file

Abbreviated: the whole tube has 8 segments in each loop, and
`tests/live/data/weldment_profiles/opus_profiles.json` holds 11 profiles in full.

```json
{"profiles": [{
  "path": "Opus/rhs-welded/80x60x2.7.sldlfp",
  "designation": "80×60×2.7", "shape": "rect_tube", "dims": {"h": 80, "b": 60, "t": 2.7},
  "area_mm2": 708.0666, "mass_per_m": 5.5583,
  "outline": {"outer": [
    {"k": "arc", "a": [-30, -34.6], "b": [-24.6, -40], "c": [-24.6, -34.6], "r": 5.4, "ccw": true},
    {"k": "line", "a": [-24.6, -40], "b": [24.6, -40]},
    ...
  ], "holes": [[...]]},
  "properties": {"Description": "80×60×2.7", "Opus code": "rhs-welded/80x60x2.7", "Material": "Steel"}
}]}
```

- `path` (required): `<standard>/<type>/<size>.sldlfp` under `root`. Folders are made as needed. A path leaving the root, with another number of parts, or with a character Windows or SolidWorks refuse (`<>:"|?*@`) is refused.
- `outline` (required), in millimetres: `outer`, a loop running counter-clockwise, and `holes`, loops running clockwise. A segment is `{"k": "line", "a": [x, y], "b": [x, y]}` or `{"k": "arc", "a", "b", "c": [cx, cy], "r": radius, "ccw": true|false}`, an arc from `a` to `b` about `c`, counter-clockwise when `ccw`. Each segment starts where the one before ends (within 0.001 mm; it is drawn from that end, so the loop closes exactly), the last ends where the first starts, an arc's ends lie on its circle (within 0.001 mm), and a full circle is two arcs. Holes lie inside the outer loop and apart from each other. The outline's origin is the sketch's origin, the member's default pierce point: a tube or bar centred on it, an angle's heel on it, a channel or beam on y = 0.
- `properties`: the file's custom properties, as text. A structural member's cut-list item takes them over.
- `area_mm2` and `mass_per_m` (steel, 7850 kg/m³) are checked against the outline's own area, which counts each arc only over its sweep: off by more than 0.2 %, the profile gets a `warning`.
- `designation`, `shape` and `dims` go into the results file. Any other key is refused.

A profile with a bad entry is refused alone, with every problem it has, before SolidWorks is called; one SolidWorks' own sketch check refuses (loops that cross themselves or each other) fails alone. Two entries for one path are both refused. A failed profile leaves no file, and a failed `overwrite` leaves the old file as it was (its error says so).

#### What comes back

Each profile's `path`, `status` (`built`; `skipped`, the file exists and `overwrite` is off; or `error`), `error`, `seconds` and the file's `sha256` (a skipped profile's: the file there), with a `warning` when its outline disagrees with its area or mass. `<spec>.results.json` beside the spec (`opus_profiles.json` gives `opus_profiles.results.json`) has the same for each profile and also `file`, `bytes`, `designation`, `shape`, `dims`, `properties`, the outline's `area_mm2`, the expected area and mass, the sketch's `definition`, the `define` it was built with and when (`at`), for an upload step. A spec built in parts keeps one results file: each call adds or replaces its own profiles, in spec order, and a profile no longer in the spec drops out. When no profile is built or skipped, the call is an error with every profile under `data`: `profiles_not_built`, or `solidworks_unavailable` when the connection was lost. A SolidWorks that stops answering, or a connection to it that broke, stops the batch: the rest are reported not built, and the error (or, after some were built, the first warning) says so.

#### The library layout

`<root>/<standard>/<type>/<size>.sldlfp`: a folder per standard and per type and a file per size, the layout of SolidWorks' libraries before 2018 (since then a file per type with a configuration per size). Each file has the one configuration of the part template (`Default` in English). `list_weldment_profiles` reads both layouts (checked live on the built root: the standard `Opus` with its 11 types and sizes); `create_structural_members` takes a profile by `standard`, `profile_type` and `size` once the root is a Weldment Profiles folder, or by `profile_path` (`size` its file name) from anywhere.

#### Pointing SolidWorks at the folder

Tools > Options > System Options > File Locations > Show folders for: **Weldment Profiles** > Add, and pick the folder that holds the standard folders, the `root` (`D:\Library\Weldment Profiles`, holding `Opus\`). With no `root`, the profiles are built in the first folder listed there, by default SolidWorks' own library (`C:\ProgramData\SOLIDWORKS\SOLIDWORKS 2025\Weldment Profiles`), beside `DIN` and `ISO`.

#### Pierce points

Each sketch has a point at the centre of the outline's box, at its four corners and four edge midpoints, and at the area centroid; points that coincide (a tube's centre and centroid) are one. Structural Member's Locate Profile picks one; `IStructuralMemberFeatureData.LocateProfilePoint` does through the API. Checked live: an RHS 80x60 pierced at its box's corner moved 30 and 40 mm, the corner along its line.

---

### `create_structural_members`

Put a weldment profile on many segments in one call: columns, beams, truss chords and web members.

**Prerequisite:** An open part

**SolidWorks automation note:** The segments are drawn as a 3D sketch (`ISketchManager.CreateLine`) or taken from an existing sketch. `IFeatureManager.InsertWeldmentFeature` comes first in a part that isn't a weldment yet, then `InsertStructuralWeldment5` with the profile file, the size as its configuration and one `IStructuralMemberGroup` per chain of segments joined end to end. SolidWorks refuses a group that branches (a truss's chords and web together); if it refuses the chains, each segment becomes its own group. The profile's `.sldlfp`, which SolidWorks leaves open, is closed again. A `profile_path` file of one size (one configuration, whatever its name) goes in with an empty configuration: SolidWorks 2025 refused it under its configuration's name, `Default` or another. Checked live on 2025 SP4 (RHS 150x100 and UPN 200 in eight directions and four chains): a member alone in its group gets its profile turned by the smallest rotation taking +z to its direction, so a rafter sloping in the xy plane comes out rolled by its slope; in a chain each next member carries the previous frame round the bend; `IStructuralMemberGroup.Angle` turns it further by the right hand. `AlignmentVector` changed nothing, so the turn that points each profile's height at `up` is computed and given as `Angle`, and one body per member direction is measured afterwards (`orientation.max_roll_deg`, its flat sides against `up`). With `define`, the line ends meeting at a node are merged (`sgMERGEPOINTS`) and every node gets its distance to the Right, Top and Front planes (`AddDimension2`, text halfway between point and plane) or a coincident relation with a plane it lies on, with auto-solve off and `CommandInProgress` on while they go in (a 6 x 6 grid: 20 s instead of 43 s). Checked live on 2025 SP4: once a 2D sketch in the part has been dimensioned, later 3D sketch points refuse their distance to the Top plane, so such a point loses its other dimensions again and gets a Fix relation; members drawn before the plates keep their dimensions.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `size` | `str` | ✅ | | Profile size as the library names it, e.g. `IPE300`, `HE400B`, `RHS100x100x5` |
| `standard` | `str?` | — | `None` | Library standard, e.g. `DIN` |
| `profile_type` | `str?` | — | `None` | Library profile type, e.g. `ipe`, `hea`, `shs` |
| `profile_path` | `str?` | — | `None` | A `.sldlfp` profile of your own instead of `standard` and `profile_type`; for a file of one size (as `build_weldment_profiles` writes), `size` is its file name |
| `segments` | `list[list[float]]?` | — | `None` | Member axes to draw as a new 3D sketch, each `[x1, y1, z1, x2, y2, z2]` in mm |
| `segments_file` | `str?` | — | `None` | A `.json` file of segments instead (a list, or `{"segments": [...]}`), such as a lattice generator writes; up to 2000 |
| `sketch` | `str?` | — | `None` | An existing sketch whose lines and arcs to use instead |
| `segment_names` | `list[str]?` | — | `None` | Only these segments of `sketch`, e.g. `["Line1", "Line4"]` |
| `corner_treatment` | `str` | — | `miter` | Where segments of one chain meet: `miter`, `butt1`, `butt2` or `none` |
| `up` | `list[float]?` | — | `None` | Which way each profile's height points, `[x, y, z]`, taken square to each member; default model y (beams, rafters and braces upright, sloped ones not rolled); a roof's normal for purlins square to it; across a column, e.g. `[1, 0, 0]` |
| `rotation` | `float` | — | `0.0` | A further turn of the profile about the member's axis, degrees, after `up` |
| `mirror_profile` | `str` | — | `none` | Mirror the profile about its `horizontal` or `vertical` axis |
| `merge_arcs` | `bool` | — | `false` | Make tangent arc segments one body |
| `grouping` | `str` | — | `auto` | `auto` groups segments joined end to end (no chain runs through a node inside a member already there, such as a column); `single` one group; `each` one per segment |
| `name` | `str?` | — | `None` | Name for the new feature |
| `sketch_name` | `str?` | — | `None` | Name for the new 3D sketch drawn from `segments`; default `<name> skeleton` |
| `define` | `str` | — | `dimensions` | `dimensions` ties every node of the new sketch to the Right, Top and Front planes (a Fix relation where SolidWorks refuses a dimension); `none` leaves it under defined |
| `folder` | `str?` | — | `None` | Feature-tree folder for the member feature and its sketch, made if missing |

**Sample call:**

```json
{"standard": "DIN", "profile_type": "hea", "size": "HE400B", "segments": [[0, 0, 0, 0, 12000, 0], [9000, 0, 0, 9000, 12000, 0]], "name": "Columns", "folder": "Frame"}
```

---

### `create_trim_extend`

Trim or extend structural members where they meet: beams to columns, joists coped to beams, braces to the frame (Trim/Extend).

**Prerequisite:** An open weldment part

**SolidWorks automation note:** `IFeatureManager.InsertWeldmentTrimFeature2(corner, options, gap, bodies, boundary)`, the bodies as `VT_DISPATCH` arrays; corner `swEndConditionTrim` (4) for an end trim or `Miter`/`Butt1`/`Butt2` (1-3) for a corner of two members, and the options `swWeldmentTrimExtendOptionType_e` flags (allow extension 1 and 2, coped cut 4, weld gap 8). A name is a member feature (every body whose faces it made) or a body. SolidWorks renames the bodies it trims after the feature (`Joists[1]` becomes `Trim/Extend1[1]`), so each member is followed by its centre of mass (and a volume at least half its own) and its new name returned; a member whose volume and centre of mass didn't move is reported as `unchanged` (a mitre keeps the volume but moves the centre). New bodies the trim leaves that copy another body or lie inside a boundary member are deleted (`InsertDeleteBody2`, a `<name> leftovers` feature: beams chained round six columns came back four of them twice, seen live); any other new body is a member cut in two, reported in `split_pieces`.

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
| `folder` | `str?` | — | `None` | Feature-tree folder; default the last folder holding the members or boundary, or a new numbered one where moving it there would put it before a later feature |

**Sample call:**

```json
{"members": ["Joists IPE160"], "boundary": ["Main beams IPE240"], "cut": "coped", "weld_gap_mm": 5}
```

---

### `check_joints`

Check a weldment's joints: members that run into each other, member ends left open, bodies touching nothing.

**Prerequisite:** An open weldment part

**SolidWorks automation note:** Overlaps come from `IModeler.CheckInterferenceBetweenTwoBodies` (byref `VARIANT` out-parameters; the intersection's volume from `IBody2.GetMassProperties`), asked only for pairs whose bounding boxes share volume: it costs about 0.1 s a pair. Touching bodies don't interfere, coincidence or not, so contact is measured: points spread over each member's planar end faces (`IFace2.GetTessTriangles`) are moved 0.2 mm along the face's outward `Normal`, and `IFace2.GetClosestPointOn` on the nearby faces of nearby bodies says whether something is within `contact_mm`. A member is a body at least 2.5 times longer than it is wide, its axis the direction its planar faces' normals avoid, its ends the planar faces facing along it. An end with under a quarter of its points touching is open (none) or partly open; a tube on one as wide as itself touches with about a third (its side walls sit on the other's corner radii). A hollow end (a ring: `IFace2.GetLoopCount` above 1) whose ring lands is closed only when four of five points across its opening find a body within 3 mm, else it is partly open (`opening_covered_percent`): a column top a sloped beam covers a sixth of. An open section's end touching nothing is a `free_end`, needing no cap. An overlap is judged lump by lump: one thinner than 0.01 mm (twice its volume over its surface area) is touching, however large (brace flats on gussets on a slope overlapped 3.6 mm³ a thousandth of a millimetre deep). An end buried in a body it overlaps counts as that overlap. Bodies left outside the largest group, or alone, are probed on their other faces too, at nine points of the box each face shares with a nearby body: a face plate long enough to count as a member touched its post only with its broad face, and a member lapped across another only with its side. About 0.2 s a body: on a 12-member frame, 4 open ends found in 9 s.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `members` | `list[str]?` | — | `None` | Only these member features or bodies: their overlaps, ends and contacts |
| `overlap_mm3` | `float` | — | `1.0` | Bodies sharing at most this volume touch rather than overlap |
| `contact_mm` | `float` | — | `0.5` | How near an end must come to another body to be closed; raise it above a trim's weld gap |
| `max_listed` | `int` | — | `25` | The most items each list gives; the counts are complete |

**Sample call:**

```json
{"members": ["Leg braces"]}
```

---

### `create_end_caps`

Close the open ends of hollow structural members with end caps (End Cap).

**Prerequisite:** An open weldment part

**SolidWorks automation note:** The end faces are selected (`IEntity.Select4`, a `VT_DISPATCH` null for the select data) and capped by one `IFeatureManager.InsertEndCapFeature3(depth, given_offset, chamfer, offset, thickness_ratio, chamfer_size, corner_treatment, depth_offset, reverse, placement)`. Checked live on an RHS100x100x4 post: placement 0 puts a 5 mm cap outside the end (the post keeps its 1000 mm), 1 flush with it (the post becomes 995 mm), 2 inside the tube at the depth offset, sized to the inside (92 x 92 mm); an offset of 3 mm insets the cap to 94 x 94 mm. `open_ends` caps what `check_joints` lists as open (free hollow ends); a partly open end is left for a bearing or cap plate and named in the `warning`. A point in the end's bore finds it: an end is near a point lying in its outline box and within 20 mm of its plane. Each cap's cut-list item gets its `Description` (`End cap PL5 90x90`) and `THICKNESS`, which SolidWorks leaves blank.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `ends` | `list[list[float]]?` | — | `None` | Points `[x, y, z]` mm at the ends to cap; each takes the member end face nearest it, within 20 mm |
| `open_ends` | `bool` | — | `false` | Cap every member end that touches nothing |
| `thickness_mm` | `float` | — | `5.0` | Cap thickness |
| `placement` | `str` | — | `inward` | `inward` flush with the end (the member gets shorter); `outward` on the end; `internal` inside the tube |
| `inset_mm` | `float` | — | `0.0` | Inset of the cap's edges from the profile's outside (room for a fillet weld) |
| `depth_mm` | `float` | — | `0.0` | `internal` caps: depth inside the end |
| `contact_mm` | `float` | — | `0.5` | With `open_ends`: how near an end must come to something to be closed |
| `name` | `str?` | — | `None` | Name for the new feature |

**Sample call:**

```json
{"open_ends": true, "thickness_mm": 5}
```

---

### `create_plates`

Add plates to a weldment in one call: base, splice, cap, gusset and connection plates, with their bolt holes.

**Prerequisite:** An open part, no sketch open

**SolidWorks automation note:** A plate whose normal runs along x, y or z is sketched on the main plane or a plane offset from it (`IFeatureManager.InsertRefPlane` with the Distance constraint; plates on one level share it). Any other plate stands on a plane square to a short line in a "Plate axes" 3D sketch, through the line's start (`InsertRefPlane` Perpendicular + Coincident, the two references marked 0 and 1). The outline and holes are drawn in the plate's own x/y, mapped into the sketch by `ISketch.ModelToSketchTransform`, then every corner and hole centre gets a horizontal and a vertical dimension from the part origin's projection and every hole its diameter, so the sketch is fully defined. `FeatureExtrusion3` with Merge off makes each plate its own body, named after the plate; its cut-list item gets `Description` (`PL15 250x250`), `MARK` and `THICKNESS`. Checked live on 2025 SP4: four 250 x 250 x 15 base plates with four Ø22 holes each and a tilted gusset with two Ø14 holes, every sketch fully defined, the base plates one cut-list item of four.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `plates` | `list[object]?` | — | `None` | The plates: `name`, `thickness` (mm), `origin` `[x, y, z]`, `normal` (the thickness direction), `outline` (corners `[x, y]` in the plate's x/y) or `width` and `height` (a rectangle centred on `origin`), and optionally `mark`, `x_axis`, `holes` (each `diameter` and `positions`) and `thickness_side` (`normal`, `reverse` or `mid`) |
| `plates_file` | `str?` | — | `None` | A `.json` file of plates instead (a list, or `{"plates": [...]}`); up to 500 |
| `folder` | `str?` | — | `Plates` | Feature-tree folder for the plates, made if missing |
| `define` | `str` | — | `dimensions` | `dimensions` fully defines each plate's sketch; `none` leaves it under defined |

**Sample call:**

```json
{"plates": [{"name": "Base plate", "mark": "BP-1", "thickness": 15, "width": 250, "height": 250, "origin": [0, 0, 0], "normal": [0, -1, 0], "holes": [{"diameter": 22, "positions": [[-85, -85], [85, -85], [85, 85], [-85, 85]]}]}]}
```

---

### `create_cuts`

Cut bolt holes, copes and notches in weldment members in one call.

**Prerequisite:** An open part with the members in, no sketch open

**SolidWorks automation note:** A cut is drawn like a plate (`create_plates`): its outline and holes in its own x/y, on a main plane, a plane offset from it (named `Cuts Y+2760`, shared by the cuts on that level) or, for a tilted cut, a plane square to a line in a "Cut axes" 3D sketch; every corner and hole centre is dimensioned from the origin and every hole's diameter, so the sketch is fully defined. `IFeatureManager.FeatureCut4` then cuts it mid-plane (`swEndCondMidPlane`) through `depth`, so it removes steel within half the depth each side of the plane and nothing further: a joist's web and the fin plate bolted to it, not the next joist. The bodies it went through are read from the cut's faces (`IFeature.GetFaces`, `IFace2.GetBody`). A hole inside the outline is refused (SolidWorks would leave it standing as an island), as are overlapping holes; a cut that reaches no steel is refused with what to check.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `cuts` | `list[object]?` | — | `None` | The cuts: `name`, `origin` `[x, y, z]` (in the middle of the steel it cuts), `normal` (the way it goes through), `depth` (mm, half each side), and an `outline` (corners `[x, y]` of the steel to remove) or `width` and `height` (an opening centred on `origin`), `holes` (each `diameter` and `positions`), or both; optionally `x_axis` |
| `cuts_file` | `str?` | — | `None` | A `.json` file of cuts instead (a list, or `{"cuts": [...]}`); up to 500 |
| `folder` | `str?` | — | `Cuts` | Feature-tree folder for the cuts, made if missing |
| `define` | `str` | — | `dimensions` | `dimensions` fully defines each cut's sketch; `none` leaves it under defined |

**Sample call:**

```json
{"cuts": [{"name": "FJ-1 end cope", "origin": [3987, 2920, 0], "normal": [0, 0, 1], "outline": [[-80, 0], [10, 0], [10, 90], [-80, 90]], "holes": [{"diameter": 18, "positions": [[-40, -40], [-40, -95]]}], "depth": 120}]}
```

---

### `pattern_members`

Copy finished members, plates or caps in a row, a grid, around an axis, or mirrored, instead of drawing each.

**Prerequisite:** An open weldment part with the original built, no sketch open

**SolidWorks automation note:** `IFeatureManager.FeatureLinearPattern5` and `FeatureCircularPattern5` with the bodies selected under mark 256 and the direction (or axis) under mark 1, a second direction under mark 2; the same patterns built through `CreateDefinition` data came back `None`. A direction along x, y or z runs on a reference axis where two main planes meet (`InsertAxis2`, named `X axis`, made once); any other runs on a fully defined line in a 3D sketch named after the pattern. SolidWorks runs a pattern from the axis's first point to its second (`IRefAxis.GetRefAxisParams`); the first copy is measured against the original anyway and the pattern made again the other way when it went backwards. Skipped instances are pairs of 0-based indexes in `ILinearPatternFeatureData.SkippedItemArray` (`VT_I4`), set through `AccessSelections` and `ModifyDefinition` with a `VT_DISPATCH` null for the component. A mirror is `InsertMirrorFeature` with the bodies (mark 256) and a main plane, one offset from it, or, for a tilted normal, a plane square to a short line drawn along the normal in a 3D sketch (mark 2), as a tilted plate's plane is made. Checked live on 2025 SP4: 25 balusters less two at a gate, a frame with its base plate 5 times along z, a mirrored brace and six spokes; every copy kept its cut-list length and end angles and joined its original's item. A pattern placed in a folder before a feature that changes its bodies (the trim of the original) would copy them untrimmed, so it goes in a new numbered folder instead.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `members` | `list[str]` | ✅ | | What to copy: member features (every body), body names, plates (`BP-1 Base plate`), end-cap features |
| `along` | `object?` | — | `None` | Copies in a row: `direction` `[x, y, z]`, `spacing` (mm), `count` (the original included) |
| `along2` | `object?` | — | `None` | With `along`: a second direction, for a grid |
| `around` | `object?` | — | `None` | Copies around an axis: `axis` `[x, y, z]`, `point` on it, `count`, `angle` (total, 360 = evenly all round) |
| `mirror` | `object?` | — | `None` | A mirrored copy: `plane` (`Front`, `Top`, `Right`) or `point` and `normal` (any direction) |
| `skip` | `list?` | — | `None` | Instances of a row to leave out, 1-based (the original is 1): `[4, 5]`, on a grid `[[2, 3]]` |
| `name` | `str?` | — | `None` | The pattern's name; default `<first member> pattern` |
| `folder` | `str?` | — | `None` | Feature-tree folder; default the last folder holding the members |

**Sample call:**

```json
{"members": ["Baluster"], "along": {"direction": [1, 0, 0], "spacing": 120, "count": 25}, "skip": [10, 11], "name": "Balusters"}
```

---

### `create_weld_beads`

Put a cosmetic fillet weld on every shop joint of a weldment, and measure the weld length.

**Prerequisite:** An open weldment part, no sketch open

**SolidWorks automation note:** The joints are found as `check_joints` finds contacts: every member end (flat or coped) landing on a body, and, for bodies touching otherwise, the face most of whose points land (a plate's edge on a member, not its side meeting the member along one line). Each joint is one `IFeatureManager.InsertCosmeticWeldBead` with the weld-from faces under mark 4 (the faces across the landing face's outer loop: a tube end's four flats and four corner radii, never its bore) and the weld-to faces under mark 8; `InsertCosmeticWeldBead2` with the face sets as arrays made nothing. The bead size is the leg, `throat_mm` × √2. Two mitred ends landing on each other are welded once. The length is the weld folder's `ICosmeticWeldBeadFolder.TotalLength` before and after each bead. Checked live on 2025 SP4: a 100×100×5 column on its base plate and under its cap plate (382.8 mm each, all round), a mitred RHS corner (462.7 mm) and a fin plate on the column (260 mm, both sides and the ends).

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `throat_mm` | `float` | ✅ | | Fillet weld throat a in mm (a4 = 4; the leg is a × 1.414) |
| `members` | `list[str]?` | — | `None` | Only the joints of these members, bodies or plates; all when omitted |

**Sample call:**

```json
{"throat_mm": 4}
```

---

### `create_vent_holes`

Cut the vent and drain holes hot-dip galvanizing needs in closed hollow members.

**Prerequisite:** An open weldment part, no sketch open

**SolidWorks automation note:** Each member body's flat faces are read (`IFace2.Normal`, `ISurface.PlaneParams`, the vertices of `IFace2.GetEdges`); the axis is square to its two largest non-parallel faces, the outer walls are the faces facing away from the axis through the body's centre of mass, and each wall's extent along the axis comes from its own vertices, so mitred and coped ends are measured, not assumed. Near the end that hangs higher (`hang`), a vent through the wall facing up; near the lower end, a drain through the wall facing down; a member hanging plumb takes two opposite walls. Each hole is a circle sketched on the wall's face (`SelectByID2` on the face, dimensioned from the origin), cut blind by `FeatureCut4` to the member's axis, so only the near wall is cut. Round tubes have no flat wall and are skipped with the reason.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `members` | `list[str]` | ✅ | | Structural member feature names (every body of them) or body names |
| `diameter` | `float` | ✅ | | Hole diameter, mm: the galvanizer's table, never under 10 mm |
| `from_end_mm` | `float` | — | `30.0` | Hole centre from the member's end, mm |
| `hang` | `list[float]` | — | `[0, 1, 0]` | Up while the piece hangs in the zinc bath |
| `folder` | `str?` | — | `Vent holes` | Feature-tree folder for the holes, made if missing |
| `define` | `str` | — | `dimensions` | `dimensions` fully defines each hole's sketch; `none` leaves it under defined |

**Sample call:**

```json
{"members": ["Rails"], "diameter": 12, "from_end_mm": 30}
```

---

### `export_plate_dxfs`

Write a DXF of every distinct plate in the weldment for the laser or plasma table, with a plates.csv list.

**Prerequisite:** An open weldment part, saved to disk

**SolidWorks automation note:** Every cut-list item whose bodies no structural member made (`IBody2.GetFeatures` has no `WeldMemberFeat`) and that has a flat face is a plate: its largest planar face is selected and written by `IPartDoc.ExportToDWG2` with `swExportToDWG_ExportSelectedFacesOrLoops`, holes included, 1:1. Thickness is the body's volume over that face's area. Checked live on 2025 SP4: the four base plates gave one `BP-1_t15_x4.dxf` of 60979.5 mm² (the plate less four Ø22 holes).

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `output_dir` | `str` | ✅ | | Folder for the DXFs and `plates.csv`, made if missing |
| `prefix` | `str?` | — | `None` | Put before every file name, e.g. the job number |

**Sample call:**

```json
{"output_dir": "C:\\steel\\OP-2026-014\\dxf", "prefix": "OP-2026-014_"}
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
