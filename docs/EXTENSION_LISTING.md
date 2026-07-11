# Node Peek — extensions.blender.org listing

> Paste the body below into the **Description** field on extensions.blender.org.
> Fill the short fields from the table first.

## Store fields

| Field | Value |
|---|---|
| Name | Node Peek |
| Tagline | Live rendered thumbnail previews above shader nodes |
| Tags | Node, Material, Render |
| License | GPL-3.0-or-later |
| Website | https://github.com/mlstr0m/node-peek |
| Support / Bug tracker | https://github.com/mlstr0m/node-peek/issues |

---

## Description (Markdown body)

**See what every node does — without plugging it into the output.**

Node Peek renders a small thumbnail above each node in the Shader Editor,
showing that node's actual output. Textures, ramps, math, mixes, and full BSDFs
all get a live preview, so you can read a material at a glance instead of
rewiring it to the Material Output over and over.

### Features

- **A preview on (almost) every node** — texture, colour and vector nodes show a
  flat 2D map; shader nodes show a lit sphere.
- **Live updates** — tweak a value and the affected previews refresh on their own.
- **Never blocks Blender** — previews are rendered in a separate background
  process, so the interface stays responsive even on heavy graphs.
- **Doesn't touch your file** — nothing is written into your .blend; open your
  scene anywhere, with or without the add-on.
- **Smart caching** — only nodes that actually changed re-render; results are
  reused across undo/redo and reverted tweaks.
- **Node-group aware** — step into a group and its interior gets previews too,
  evaluated with that instance's real inputs.
- **Light on resources** — thumbnails render at 1 sample where possible, on a
  capped number of threads, at low process priority.

### How it works

When your material changes, Node Peek hands a copy of just that material to a
persistent background Blender running in `--background` mode. It renders each
changed node's output to a small image, which the add-on then draws above the
node with the GPU. Your actual scene is never modified and never re-rendered.

### Usage

1. Open a **Shader Editor** and select an object with a node-based material.
2. Previews appear above the nodes automatically.
3. Open the **N-panel → Node Peek** for controls (resolution, refresh, clear).
4. Prefer to choose nodes yourself? Turn off *Previews Visible By Default* and
   press **Ctrl+Shift+P** to toggle previews on the selected nodes.

### Requirements

- Blender **4.2 or newer**.
- Previews render with **Cycles** in the background process (this is reliable
  headless; EEVEE is available as an experimental option but often renders black
  without a GPU context).

### Good to know

- Previews render the **surface** shader. A material driven by the Material
  Output **Displacement** input shows its bump/normal detail but not the
  displaced silhouette.
- Repainting a **packed** image doesn't auto-invalidate its preview — use
  *Refresh Previews*.
- The view transform is fixed to **Standard**, so previews don't match an
  AgX/Filmic look.

### Open source

GPL-3.0-or-later. Source, issues and contributions:
**https://github.com/mlstr0m/node-peek**

Built by **mlstr0m** — [Résidence Principale](https://residenceprincipale.net).

*Independent, clean-room implementation inspired by the idea behind the
commercial "Node Preview" add-on. It shares none of that product's code.*
