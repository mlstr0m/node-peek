# Changelog

## 0.6.0

**Previews now follow your selection by default.** This changes the behaviour
you are used to. If you want the old one back, set *Show Previews* to
**All Nodes** in the Overlays popover or in the sidebar.

### Why

Reported by a user on Blender Extensions: in dense node trees the previews
covered the neighbouring nodes, and the only way out was to turn previews off
entirely. Measured on a 28-node layered shader, previewing every node covered
**36% of the total node area**, with 19 of 28 nodes more than a quarter hidden.
Previewing only the selection brings that down to **1.3%**.

### Added

- **Show Previews** display mode: *Selected Nodes* (new default) or *All Nodes*.
  In *Selected Nodes*, deselecting everything leaves the graph completely clean.
- Node Peek controls in the Shader Editor's **Overlays popover**, where a
  preview overlay belongs.
- Preview **pinning**. `Ctrl+Shift+P` keeps a preview visible after the node is
  deselected, so you can watch one node's result while editing another. A new
  *Unpin All* button clears every pin.
- A hint in the sidebar when nothing is selected, instead of an unexplained
  empty graph.

### Changed

- Previews now respect the editor's **Overlays** toggle. Turning overlays off
  hides them, per editor, like every other overlay.
- `Ctrl+Shift+P` now pins previews instead of enabling/disabling them.
- The *Previews Visible By Default* preference is replaced by *Show Previews*.

### Fixed

- Manually enabled previews leaked between materials. The key was
  `tree_name/node_name`, and every material's embedded tree is named
  `Shader Nodetree`, so enabling a preview on a node also enabled it on
  same-named nodes in every other material. Pins are now keyed by material,
  group-instance path and node name.

## 0.5.6

- Convert preview image colours to sRGB (fixes dark previews, PR #3 by
  mumu-lhl).
- Per-node preview sizing.
- Normalize only out-of-range preview data.
