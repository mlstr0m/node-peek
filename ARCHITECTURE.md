# Node Peek — Architecture

This document explains how Node Peek works internally, the invariants that keep
it safe, and where to look when something breaks. Read it before changing
anything non-trivial.

## The big picture

Node Peek is **two processes**:

```
┌───────────────────────────────┐         ┌──────────────────────────────────┐
│  Blender (the user's session) │         │  Worker: blender --background    │
│  __init__.py                  │         │  --factory-startup  worker.py    │
│                               │         │                                  │
│  depsgraph handler ──┐        │  stdin  │  readline() ── blocks idle       │
│  timer (0.3s) ───────┼─ debounce ─────▶ │  1 JSON line = 1 job             │
│  fingerprint gate ───┘        │         │                                  │
│                               │         │  hash every node                 │
│  material.blend  ◀── libraries.write    │  render only cache misses        │
│                               │         │  (Cycles CPU, tiny frames)       │
│  _collect_results() ◀──────── response.json (streamed, atomic writes)      │
│  PNG → GPU texture            │         │  PNGs → content-addressed cache  │
│  draw handler (POST_PIXEL)    │         │                                  │
└───────────────────────────────┘         └──────────────────────────────────┘
```

- The **main process** never renders. It detects edits, sends jobs, loads
  finished PNGs into GPU textures, and draws them above nodes.
- The **worker** is one long-lived headless Blender. It receives jobs on
  stdin, renders each previewable node's output to a small PNG, and streams
  results back through `response.json`.

Files on disk (all under the OS temp dir):

| Path | Written by | Purpose |
|---|---|---|
| `node_peek_job_*/material.blend` | main | just the material + deps (`libraries.write`) |
| `node_peek_job_*/response.json` | worker | streamed job results (atomic tmp+rename) |
| `node_peek_job_*/worker.log` | worker | the worker's log (main's stdout goes to DEVNULL) |
| `node_peek_cache/<hash>.png` | worker | thumbnail cache, content-addressed |
| `node_peek_cache/*.linear.exr` | worker | short-lived float render used only to inspect data range |

The job dir is per-session (`mkdtemp`) and deleted on unregister. The cache dir
is shared across sessions and instances — safe because filenames are content
hashes and writes are atomic (`.tmp` + `os.replace`).

## Core invariants — do not break these

1. **The user's .blend is never modified.** We only *read* the material and
   serialise it with `bpy.data.libraries.write` to a temp file. All destructive
   graph surgery (rewiring sockets, adding helper nodes, mutating group
   interfaces) happens in the worker, on a **throwaway copy** that is purged
   after each job.
2. **The worker only ever executes Blender itself** (`bpy.app.binary_path`),
   with `shell=False`, running the bundled `worker.py`. No other executable, no
   network, no downloaded code. This was a review point on
   extensions.blender.org — keep it true.
3. **Cache writes are atomic.** Render to `<hash>.<pid>.tmp.png`, then
   `os.replace` into `<hash>.png`. A killed worker can never leave a corrupt
   file under a final name; two Blender instances can share the cache.
4. **Every job gets a `done` response, no matter what.** The main loop of the
   worker wraps `process()` and writes an empty done-response on failure.
   Without this, the main process would wait forever on that seq.
5. **A failed preview degrades to "no thumbnail", never to a crash or a wrong
   thumbnail.** Any per-node exception pops the node from the response and
   continues.

## Main process (`__init__.py`)

### Update pipeline (state machine)

State lives in module globals (reset on file load / re-register):

- `_dirty` — a depsgraph event touched a Material/ShaderNodeTree.
- `_last_edit_time` — debounce anchor (pref `debounce`, default 0.4 s).
- `_pending_seq` / `_consumed_seq` — request in flight when they differ.
- `_worker_failures` — circuit breaker; at 3 consecutive failures we stop
  auto-retrying (Refresh resets it).
- `_progress_time` — last time the in-flight job streamed progress; used by
  the stuck-job watchdog (`_STUCK_TIMEOUT`, 90 s without progress → restart).

The timer `_poll_timer` (0.3 s idle / 0.15 s while waiting) drives everything:

```
collect results → watchdog (dead/stuck worker) → auto-kick on material/path
change → if dirty & debounced & not waiting: _request_render()
```

`_request_render` computes a **fingerprint** of the material tree
(`_material_fingerprint`) and skips dispatch entirely when nothing
preview-relevant changed — this is what makes node *selection* free. The
fingerprint also encodes resolution, engine, and the group path, so changing
any of those re-dispatches.

**Keep in sync:** `_FP_SKIP_PROPS` (main) and `_HASH_SKIP_PROPS` (worker) skip
the same cosmetic properties. If they drift, either edits stop triggering
updates (main too loose) or dispatches never produce renders (worker cache
hits). Same for `PATH_SEP` on both sides.

### Texture lifecycle

- `_tex_by_path`: png path → `GPUTexture`, shared by all nodes with the same
  content hash.
- `_img_by_path`: png path → the `bpy.types.Image` datablock **kept alive on
  purpose**: `gpu.texture.from_image()` does not guarantee the texture survives
  its source image. Images are dot-prefixed (`.abc123.png`) so they stay out of
  UI lists, and are removed exactly when their texture is dropped.
- `_node_pngpath`: preview key → png path. Keys are node names at top level,
  `"<group name(s)>\x00<node name>"` inside groups.
- On **File → Open** (`_on_load_post`): every datablock reference we hold is
  dead. Drop all dicts *without* touching the datablocks, and mark any in-flight
  seq as consumed so the previous file's response is discarded when it arrives.

### Streaming consumption (`_collect_results`)

The worker rewrites `response.json` after *every* render with the full
node→png map, a `fresh` list (pngs rendered this job) and a `done` flag.
The main process:

- applies partials as they come (`fresh` entries are (re)loaded from disk once
  per job — that's also how force-refresh reloads same-path files);
- keeps the **previous** thumbnail for nodes whose new png doesn't exist yet
  (no flicker to blank);
- only marks the request consumed on `done`, then deletes the file so idle
  ticks stop re-parsing it.
- an unreadable png is deleted (it would otherwise be a permanent cache hit)
  and the job is re-kicked. The OCIO/colorspace assignment is deliberately
  tolerant so a custom OCIO config can't be mistaken for a corrupt file.

### Drawing (`_draw_callback`)

POST_PIXEL handler on `SpaceNodeEditor`. Guards, in order: shader editor with a
tree; `space.id` is the Material the textures belong to (pinned editors on
other materials draw nothing); the group path prefix namespaces keys so
same-named nodes in groups can't pick up top-level thumbnails.

Two placement subtleties, both hard-won:

- **UI scale**: `node.location` is in unscaled node units; the editor draws at
  `ui_scale`. We self-calibrate with `node.dimensions.x / node.width` (exact),
  falling back to the preference. Without this, previews drift on Retina.
- **Individual sizing**: `_preview_scale_by_key` stores only non-default
  runtime scales, keyed by material + group-instance path + node name. Drawing
  keeps each scaled square centred above its node. The selection slider updates
  this dictionary and redraws the editor; it never triggers rendering or writes
  an ID property, so changing preview size cannot dirty the `.blend`.
- **`TRI_STRIP`, not `TRI_FAN`**: Metal (macOS) doesn't support TRI_FAN — draws
  silently nothing.

## Worker (`worker.py`)

### Job pipeline (`process`)

1. `load_material` — import the material from `material.blend`.
2. `adopt_cached_images` — swap freshly loaded Image datablocks for cached ones
   (key: abspath + mtime + colorspace + alpha). Big textures are decoded once,
   not per edit. Cache capped at 16, fake-user'd so `purge_material` spares them.
3. Hash every previewable node (`node_signature`) → `<digest>.png`. The digest
   covers the node, everything upstream, node-group internals
   (`group_signature`), image file mtimes, and a salt
   (`res|engine|SCENE_VERSION|normalization`). The normalization suffix applies
   only to non-shader targets, so shader previews remain reusable across modes.
4. If a group path was sent, hash that group's interior too, salted with the
   **instance signature** (the group node chain), so two instances of the same
   group with different inputs cache separately.
5. Cache misses go into a render queue, sorted: user's active/selected nodes
   first, then flat maps (cheap) before spheres.
6. For each target: wire its output socket into the preview output —
   - flat values go through a helper **Emission** node (1 sample, exact);
   - shader sockets go straight to Surface (16 samples adaptive + denoise);
   - in-group sockets are **bubbled** to the material tree by adding temporary
     interface output sockets at each nesting level (`bubble_socket`), torn
     down in a `finally`.
   When data normalization is enabled, the worker reads the scene-linear float
   EXR and remaps only RGB channels whose min/max falls outside 0–1. In-range
   channels and all shader renders bypass normalization; the EXR is deleted
   immediately after PNG conversion.
   Render to tmp, `os.replace` into cache, stream a partial response.
7. `purge_material` + final `done` response.

### Add-on custom nodes (stub types)

The worker runs `--factory-startup`, so nodes defined by other add-ons
(`ShaderNodeCustomGroup` subclasses) would load as `NodeUndefined`: their
internal tree becomes unreachable and everything downstream evaluates flat.
The fix (`ensure_stub_types`): the main process sends the `bl_idname` of every
custom group node in the material; before loading, the worker registers an
**empty** `ShaderNodeCustomGroup` stub per idname. Blender binds nodes to types
by idname at load time, so the node becomes a functional group again — its
internal tree is already in the lib as a dependency, and Cycles renders custom
groups natively. No graph surgery, no code from the user's file is executed
(idnames are plain strings; the class body is ours). A stub that fails to
register degrades to the old behaviour for that type only. Nodes that stay
undefined (pure-Python nodes, other engines' nodes) are skipped rather than
given a misleading flat thumbnail — they don't render in the user's own
Cycles render either.

### Why the odd-looking choices

- **Cycles CPU, not EEVEE**: EEVEE renders black in `--background` (no GL/Metal
  context). CPU beats GPU spin-up at 150 px frames.
- **`use_persistent_data` OFF**: we rewire the tree between renders inside one
  job; if Cycles ever missed a re-sync, one node's result could be cached under
  another node's hash — silent, sticky corruption. Not worth the sync savings.
- **stdin, not file polling**: blocking `readline()` = zero idle wake-ups, and
  EOF doubles as crash detection for the parent (no PID watching needed).
- **The emission helper is created *after* snapshotting the node list**, so it
  never previews itself.
- **Normalization happens directly on Render Result pixels, not through the
  Compositor.** This avoids Blender-version-specific scene routing and lets the
  worker leave maps already inside 0–1 exactly unchanged.
- **`SCENE_VERSION`**: bump it whenever you change the preview scene (lights,
  cameras, sphere UVs, world). It's part of the cache salt, so old thumbnails
  invalidate automatically — never ship a scene change without bumping it.

## Failure modes and how they recover

| Failure | Detection | Recovery |
|---|---|---|
| Worker crashes mid-job | `waiting and proc.poll() is not None` | auto-restart, ≤3 attempts, then circuit-breaks with a panel message |
| Render hangs | no streamed progress for 90 s | worker killed, same retry path |
| Worker fails to answer | impossible by design (invariant 4) | — |
| Corrupt/unreadable png | image load fails | png deleted, job re-kicked |
| Blender crashes | worker's stdin hits EOF | worker exits on its own |
| File → Open mid-job | `load_post` marks seq consumed | stale response discarded |
| Stale `.tmp.png` / old cache | `_prune_cache` at worker start | tmp >1 h and png >14 days deleted |

## Debugging guide

- **Console** (main process): all `[Node Peek]` prints — worker start (with the
  log path), restarts, circuit breaker.
- **`worker.log`** (path printed at startup): one line per job —
  `[job N] X rendered, Y cached` — plus per-node render failures. If the worker
  dies instantly, this file tells you why; `stdin closed, exiting` right after
  start means the pipe never survived (platform issue).
- **Previews never appear**: is the worker alive? (`blender --background` in a
  process list). Is the panel showing "Worker keeps crashing"? Check the log.
- **Previews in the wrong place**: UI-scale calibration (see drawing section).
- **Stale previews**: hash didn't change when it should — look at
  `node_signature` / `_material_fingerprint` for the missing property, and
  remember the keep-in-sync sets.

## Known limitations (accepted, documented in README)

- One group path at a time: with two editors open at different depths, the
  first matching editor wins.
- A group instanced several times in the *same* parent tree resolves to the
  first instance (`space.path` doesn't expose which instance was entered).
- Tab-editing *inside an add-on custom group node* shows no interior previews:
  stub instances don't sync their sockets with interface changes, so the
  socket-bubbling used for in-group previews bails out (deliberately — a wrong
  thumbnail would be worse than none).
- Displacement output isn't previewed (surface only).
- Packed-image repaints don't invalidate (no mtime); Refresh does.
- View transform is Standard; AgX/Filmic looks differ.
- Deeply chained graphs (~500+ serial nodes) could hit Python's recursion limit
  in `node_signature`; the worker fail-safe turns that into "no previews" plus
  a log line, not a crash of the user's session.

## Release checklist

1. Bump `version` in **both** `blender_manifest.toml` and `bl_info`.
2. If the preview scene changed, bump `SCENE_VERSION` in `worker.py`.
3. `blender --command extension build --source-dir node_peek --output-dir .`
4. `blender --command extension validate node_peek-<ver>.zip`
5. Commit, tag `v<ver>`, GitHub release with the built zip.
6. Upload the same zip on extensions.blender.org.
