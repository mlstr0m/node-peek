# Node Peek — tests

## End-to-end: custom node support (automated, headless)

Simulates an add-on that registers `ShaderNodeCustomGroup` nodes (per-instance
trees, property-driven rebuild, shared trees, a custom node inside a regular
group, a pure-Python unrenderable node), then drives the **real worker** with
two sequential jobs and checks every preview.

```sh
BL=/Applications/Blender.app/Contents/MacOS/Blender   # your blender binary
DIR=$(mktemp -d)
"$BL" --background --factory-startup --python tests/make_real_lib.py -- "$DIR"
python3 tests/e2e_real.py "$BL" worker.py "$DIR"
```

Expected: 8 × `PASS`, ending with `E2E_REAL_OK`. The printed PNG paths can be
inspected by eye (coarse vs fine checker, gradient, sphere).

What it verifies:

- both instances of a custom type get **distinct** thumbnails (per-instance trees);
- a custom node **inside a group** contributes to the group's thumbnail;
- built-in nodes **downstream of a custom node** render correctly;
- pure-Python nodes (no internal tree) are skipped, not given a flat thumbnail;
- a bogus stub idname doesn't break the job;
- a **plain material** in the same worker session takes the unchanged fast path.

## Manual: live check in your own Blender

Open `tests/manual_test_custom_nodes.py` in Blender's Text Editor and Run,
with an object selected and Node Peek enabled. A "NP Test Checker" custom node
is added to the active material: its thumbnail (and the BSDF's) should show the
checker, and dragging its **Scale** slider should refresh both within ~1 s.
