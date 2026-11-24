# Feature Tree

What a CAD specialist sees first when they open a model: the feature tree.
A part built by many tool calls is a long flat list of `Sketch12`, `Plane7`
and `Boss-Extrude31`, every sketch under defined, so anything dragged moves.
`set_feature_folders` renames features and groups them in folders;
`check_feature_tree` measures the tree and says what to fix. Run `check_feature_tree` at
the end of every build and before a model is handed over.

### `set_feature_folders`

Rename features and group them into feature-tree folders.

**Prerequisite:** An open part or assembly

**SolidWorks automation note:** Renames set `IFeature.Name` and are read back. A new folder takes the selected features (`IFeatureManager.InsertFeatureTreeFolder2(swFeatureTreeFolder_Containing)`); features go into an existing one with `IModelDocExtension.ReorderFeature(name, folder, swMoveToFolder)`, or right after the folder's last feature, and the folder's contents (`IFeatureFolder.GetFeatures`) are read back to report what moved. Checked live on 2025 SP4: a brace added after the plates moved up into the members' folder past the plates' folder.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `rename` | `dict[str, str]?` | — | `None` | Old name to new name, applied before the folders |
| `folders` | `list[object]?` | — | `None` | Folders to fill, each `{"name": ..., "features": [...]}` in tree order |

**Sample call:**

```json
{"rename": {"Plane7": "Splice plane 3"}, "folders": [{"name": "Bracing", "features": ["Brace skeleton", "Brace"]}]}
```

---

### `check_feature_tree`

Audit the active part's feature tree as a CAD specialist would.

**Prerequisite:** An open part

**SolidWorks automation note:** Walks the top-level features (`IModelDoc2.FirstFeature`, `IFeature.GetNextFeature`), leaving out the tree's own folders, the origin and the three main planes. Each sketch's definition comes from `ISketch.GetConstrainedStatus` (`swConstrainedStatus_e`); folder contents from `IFeatureFolder.GetFeatures`; a sketch absorbed by the feature that uses it (an extrusion's profile) counts as inside that feature. Names like `Sketch12` or `Structural Member4` are SolidWorks' own. Checked live: both agent-built scarecrow weldments of the 2026-11-24 benchmark had every sketch (75) under defined and no folders.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `max_listed` | `int` | — | `20` | The most names listed per finding |

**Sample call:**

```json
{"max_listed": 20}
```
