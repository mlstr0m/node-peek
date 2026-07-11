# Node Peek — notes for the extension reviewers

> Paste this into the **note to reviewers** field when submitting on
> extensions.blender.org. It explains the one unusual behaviour up front.

Thanks for reviewing! Node Peek draws a rendered thumbnail above each node in
the Shader Editor. Everything is open source: https://github.com/mlstr0m/node-peek

## The one thing to look at: a background subprocess

To keep the UI responsive, previews are **not** rendered in the main Blender
process. Instead the add-on launches **one** long-lived helper process and feeds
it jobs.

- The command launched is **Blender itself** and only Blender:
  `subprocess.Popen([bpy.app.binary_path, "--background", "--factory-startup",
  "--python", <bundled worker.py>, "--", ...])`.
  See `_ensure_worker()` in `__init__.py`. It never runs any other executable,
  never a shell (`shell=False`), and never a path taken from user/file content.
- The worker script is the bundled `worker.py`. No code is downloaded, generated,
  or `eval`'d. There is **no network access** at all (we declare no `network`
  permission).
- Communication is local only: requests are written to the helper's **stdin**;
  results are small PNGs + a `response.json` in a temp directory.
- The helper exits automatically on EOF (parent gone) or a stop message, and is
  terminated in `unregister()`.

## Filesystem use (declared `files` permission)

- Writes only under the OS temp directory (`tempfile.gettempdir()` /
  `tempfile.mkdtemp`): a small `material.blend` handed to the worker, and a PNG
  thumbnail cache. Old temp files are pruned on startup.
- The user's `.blend` is **never** written or marked dirty. To render a material
  we serialise just that datablock with `bpy.data.libraries.write(...)`.

## Other notes

- No telemetry, no analytics, no auto-update, no bundled binaries or wheels.
- Pure `bpy` / `gpu` / stdlib. Single package, two files (`__init__.py`,
  `worker.py`).
- GPL-3.0-or-later; SPDX headers in both source files.
- Clean-room work; shares no code with the commercial "Node Preview" add-on.

## How to test quickly

1. Install, open a Shader Editor, select the default Cube (its material) or add
   a few texture/mix nodes.
2. Thumbnails appear above the nodes within a second or two (first run fills the
   cache). Tweak a value — the edited node updates first.
3. `N-panel → Node Peek` has Refresh / Clear and a resolution slider.
4. The helper process is visible as a second `blender --background` while a
   Shader Editor with a material is open; it idles at ~0 CPU (blocked on stdin)
   and is cleaned up when the add-on is disabled.
