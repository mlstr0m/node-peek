# SPDX-License-Identifier: GPL-3.0-or-later
# Node Peek — live rendered thumbnails above shader nodes.
# Copyright (C) 2026 Aurélien — Résidence Principale (mlstr0m).
"""
Node Peek
=================

Renders a small thumbnail above every node in the Shader Editor showing the
output of that node. Previews are computed by a *separate* Blender process
launched in ``--background`` mode, so the UI stays responsive, and nothing is
written into your .blend file.

This is an independent, clean-room implementation inspired by the commercial
"Node Preview" add-on. It does not contain any of that product's code.
"""

bl_info = {
    "name": "Node Peek",
    "author": "mlstr0m (Résidence Principale)",
    "version": (0, 4, 0),
    "blender": (4, 2, 0),
    "location": "Shader Editor > Sidebar (N) > Node Peek  /  Ctrl+Shift+P",
    "description": "Rendered thumbnail previews above shader nodes, computed in a background process.",
    "category": "Node",
}

import os
import json
import time
import shutil
import hashlib
import tempfile
import subprocess

import bpy
import gpu
from bpy.app.handlers import persistent
from gpu_extras.batch import batch_for_shader

# ---------------------------------------------------------------------------
# Runtime state (never persisted to the .blend)
# ---------------------------------------------------------------------------

ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER_SCRIPT = os.path.join(ADDON_DIR, "worker.py")

# restart the worker if an in-flight job streams no progress for this long
# (a real render hang; steady progress keeps resetting the clock)
_STUCK_TIMEOUT = 90.0

# separates the group-instance path from the node name in a preview key.
# NUL can't appear in a Blender ID name. Keep in sync with PATH_SEP in worker.py.
PATH_SEP = "\x00"

# GPU textures are shared per png (content-addressed), nodes map onto them.
# The Image datablocks are kept alive on purpose: gpu.texture.from_image()
# does not guarantee the texture survives its source image, so we only free
# an image when its texture is dropped.
_tex_by_path = {}     # png path -> gpu.types.GPUTexture
_img_by_path = {}     # png path -> bpy.types.Image (kept alive, dot-named)
_node_pngpath = {}    # node name -> png path
_textures_material = None  # material name the current previews belong to

# set of "tree_name/node_name" the user explicitly enabled (manual mode)
_enabled_nodes = set()

# persistent background worker + IPC state
_worker = {
    "proc": None,        # the long-lived Blender --background process
    "job_dir": None,     # response.json + material.blend live here
    "cache_dir": None,   # <hash>.png render cache (shared across jobs)
}
_req_seq = 0             # last request id we sent
_pending_seq = -1        # request awaiting a *final* (done) response
_consumed_seq = -1       # last final response we already applied
_force_next = False      # next request ignores the cache (Refresh button)
_worker_failures = 0     # consecutive worker deaths; circuit-breaks at 3
_reload_seq = -1         # seq whose freshly-rendered pngs we track
_reloaded = set()        # png names already (re)loaded for _reload_seq
_progress_time = 0.0     # last time the in-flight job showed progress
_last_path = None        # group-instance path (list of node names) last sent

_dirty = False           # a depsgraph edit happened, re-render is pending
_last_edit_time = 0.0    # for debouncing
_last_material = None    # name of the material we last kicked a render for
_last_fingerprint = None  # tree fingerprint of the last dispatched request
_draw_handle = None      # SpaceNodeEditor draw handler


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------

def _pref_rerender(_self, _context):
    """Changing resolution/engine must trigger a re-render."""
    global _dirty, _last_edit_time, _last_fingerprint
    _last_fingerprint = None
    _dirty = True
    _last_edit_time = 0.0


def _pref_redraw(_self, _context):
    _tag_redraw_node_editors()


class NODEPEEK_Preferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    resolution: bpy.props.IntProperty(
        name="Thumbnail Resolution",
        description="Pixel size of each rendered preview (higher = sharper but slower)",
        default=150, min=32, max=512,
        update=_pref_rerender,
    )
    visible_by_default: bpy.props.BoolProperty(
        name="Previews Visible By Default",
        description="Show a preview above every node. When off, use Ctrl+Shift+P "
                    "to toggle previews on the selected nodes only",
        default=True,
        update=_pref_redraw,
    )
    debounce: bpy.props.FloatProperty(
        name="Update Delay (s)",
        description="Wait this long after the last edit before re-rendering",
        default=0.4, min=0.0, max=3.0,
    )
    engine: bpy.props.EnumProperty(
        name="Render Engine",
        items=[
            ('CYCLES', "Cycles (recommended)",
             "Renders reliably in the background process"),
            ('EEVEE', "EEVEE (experimental)",
             "Faster, but often renders black in background mode"),
        ],
        default='CYCLES',
        update=_pref_rerender,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "visible_by_default")
        layout.prop(self, "resolution")
        layout.prop(self, "debounce")
        layout.prop(self, "engine")
        layout.label(text="Toggle preview on selected nodes: Ctrl+Shift+P", icon='INFO')


def _prefs():
    return bpy.context.preferences.addons[__name__].preferences


# ---------------------------------------------------------------------------
# Node geometry helpers
# ---------------------------------------------------------------------------

def _absolute_location(node):
    """Node location accumulated through parent frames (view space)."""
    x, y = node.location.x, node.location.y
    p = node.parent
    while p is not None:
        x += p.location.x
        y += p.location.y
        p = p.parent
    return x, y


def _should_preview(tree, node, visible_default):
    if node.type in {'FRAME', 'REROUTE', 'GROUP_INPUT', 'GROUP_OUTPUT'}:
        return False
    if node.type in {'OUTPUT_MATERIAL', 'OUTPUT_WORLD', 'OUTPUT_LIGHT'}:
        return False
    if not node.outputs:
        return False
    if node.hide:
        # previews on collapsed nodes look odd; skip for now
        return False
    if visible_default:
        return True
    return f"{tree.name}/{node.name}" in _enabled_nodes


# ---------------------------------------------------------------------------
# GPU drawing
# ---------------------------------------------------------------------------

def _draw_callback():
    context = bpy.context
    space = context.space_data
    if not space or space.type != 'NODE_EDITOR':
        return
    if space.tree_type != 'ShaderNodeTree':
        return
    tree = space.edit_tree
    if tree is None or not _node_pngpath:
        return

    # The previews belong to ONE material. Don't draw them on another material
    # (pinned editors, other windows). Node groups ARE drawn: the key prefix
    # below namespaces them so same-named nodes don't pick up wrong thumbnails.
    id_ = space.id
    if not isinstance(id_, bpy.types.Material):
        return
    if _textures_material and id_.name != _textures_material:
        return

    # previews are keyed by the group-instance path they were rendered for,
    # so nodes inside a group don't collide with same-named top-level nodes
    names = _path_names(space)
    key_prefix = (PATH_SEP.join(names) + PATH_SEP) if names else ""

    region = context.region
    v2d = region.view2d

    shader = gpu.shader.from_builtin('IMAGE')
    gpu.state.blend_set('ALPHA')

    pad = 4  # pixels between node top and preview
    visible_default = _prefs().visible_by_default

    # Node locations are stored in unscaled "node units"; the editor draws them
    # multiplied by the UI scale factor (DPI). We must apply the same factor
    # before converting to region pixels, or previews drift with distance from
    # the origin (very visible on Retina/HiDPI screens).
    ui_scale = bpy.context.preferences.system.ui_scale

    for node in tree.nodes:
        if not _should_preview(tree, node, visible_default):
            continue
        tex = _tex_by_path.get(_node_pngpath.get(key_prefix + node.name))
        if tex is None:
            continue

        # self-calibrate the scale from the node itself when possible; this is
        # exact and independent of the DPI preference reading.
        factor = ui_scale
        if node.width > 0 and node.dimensions.x > 0:
            factor = node.dimensions.x / node.width

        loc_x, loc_y = _absolute_location(node)
        # top-left corner of the node in region pixels
        x0, y0 = v2d.view_to_region(loc_x * factor, loc_y * factor, clip=False)
        x1, _ = v2d.view_to_region((loc_x + node.width) * factor,
                                   loc_y * factor, clip=False)
        w = x1 - x0
        if w < 8:  # too zoomed out to bother
            continue

        bottom = y0 + pad
        top = bottom + w  # square, matches node width

        # TRI_STRIP (not TRI_FAN) — TRI_FAN is unsupported on the Metal backend
        # used on macOS, which would make previews silently not draw.
        batch = batch_for_shader(
            shader, 'TRI_STRIP',
            {
                "pos": ((x0, bottom), (x1, bottom), (x0, top), (x1, top)),
                "texCoord": ((0, 0), (1, 0), (0, 1), (1, 1)),
            },
        )
        shader.bind()
        shader.uniform_sampler("image", tex)
        batch.draw(shader)

    gpu.state.blend_set('NONE')


def _tag_redraw_node_editors():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'NODE_EDITOR':
                area.tag_redraw()


# ---------------------------------------------------------------------------
# Material fingerprint (main-process side)
#
# A cheap hash of the node tree used to SKIP dispatching when a depsgraph
# event didn't actually change anything previews depend on (e.g. selecting a
# node). Kept intentionally aligned with the worker's node hashing — see
# _HASH_SKIP_PROPS in worker.py.
# ---------------------------------------------------------------------------

_FP_SKIP_PROPS = {"rna_type", "name", "label", "location", "width",
                  "height", "width_hidden", "select", "show_options",
                  "show_preview", "show_texture", "dimensions",
                  "internal_links", "hide", "color", "use_custom_color"}


def _socket_value(sock):
    try:
        v = sock.default_value
        if hasattr(v, "__len__"):
            return tuple(round(float(x), 5) for x in v)
        if isinstance(v, float):
            return round(v, 5)
        return v
    except Exception:
        return None


def _material_fingerprint(mat):
    h = hashlib.md5()

    def add(s):
        h.update(s.encode("utf-8", "replace"))
        h.update(b"|")

    seen = set()

    def walk(tree):
        if tree is None or tree.name_full in seen:
            return
        seen.add(tree.name_full)
        for node in tree.nodes:
            add(node.name)  # node names key the response, renames must re-send
            add(node.bl_idname)
            add(str(node.mute))
            for prop in node.bl_rna.properties:
                pid = prop.identifier
                if pid in _FP_SKIP_PROPS:
                    continue
                if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'ENUM', 'STRING'} \
                        and not prop.is_readonly:
                    try:
                        val = getattr(node, pid)
                        if getattr(prop, "is_array", False):
                            val = tuple(val)
                        add(f"{pid}={val}")
                    except Exception:
                        pass
            ramp = getattr(node, "color_ramp", None)
            if ramp is not None:
                add(f"{ramp.color_mode}:{ramp.interpolation}")
                for e in ramp.elements:
                    add(f"{e.position:.5f}{tuple(e.color)}")
            mapping = getattr(node, "mapping", None)
            if mapping is not None and hasattr(mapping, "curves"):
                for curve in mapping.curves:
                    for pt in curve.points:
                        add(f"{tuple(pt.location)}{pt.handle_type}")
            image = getattr(node, "image", None)
            if image is not None:
                add(f"{image.name}:{image.source}:{image.filepath}")
            for inp in node.inputs:
                if not inp.is_linked:
                    add(repr(_socket_value(inp)))
            subtree = getattr(node, "node_tree", None)
            if subtree is not None:
                walk(subtree)
        for link in tree.links:
            try:
                add(f"{link.from_node.name}.{link.from_socket.identifier}"
                    f">{link.to_node.name}.{link.to_socket.identifier}"
                    f":{link.is_muted}")
            except Exception:
                pass

    add(mat.name)
    walk(mat.node_tree)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Background rendering
# ---------------------------------------------------------------------------

def _active_material(context):
    obj = context.active_object
    if obj is None:
        return None
    mat = obj.active_material
    if mat is None or mat.node_tree is None:
        return None
    return mat


def _path_names(space):
    """The stack of group-node names you have navigated into, from the material
    tree inward. Empty at the top level. Each level resolves the (first) group
    node in the parent tree whose node_tree is the child — so a group instanced
    many times resolves to a single representative instance."""
    path = getattr(space, "path", None)
    if not path or len(path) <= 1:
        return []
    names = []
    for i in range(1, len(path)):
        parent = path[i - 1].node_tree
        child = path[i].node_tree
        if parent is None or child is None:
            break
        gn = next((n for n in parent.nodes
                   if getattr(n, "node_tree", None) == child), None)
        if gn is None:
            break
        names.append(gn.name)
    return names


def _find_shader_editor(mat):
    """The Shader Editor space showing ``mat`` and the group path currently open
    in it. Returns (space, path_names) or (None, []). Requiring a match with the
    material we actually render keeps the path resolvable in the worker."""
    wm = bpy.context.window_manager
    if wm is None or mat is None:
        return None, []
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type != 'NODE_EDITOR':
                continue
            for space in area.spaces:
                if (space.type == 'NODE_EDITOR'
                        and space.tree_type == 'ShaderNodeTree'
                        and space.id == mat  # '==' not 'is': bpy re-wraps IDs
                        and space.edit_tree is not None):
                    return space, _path_names(space)
    return None, []


def _is_worker_alive():
    proc = _worker.get("proc")
    return proc is not None and proc.poll() is None


def _prune_cache(cache_dir):
    """Startup hygiene: drop leftover temp renders and very old thumbnails."""
    now = time.time()
    try:
        entries = os.listdir(cache_dir)
    except OSError:
        return
    for fn in entries:
        path = os.path.join(cache_dir, fn)
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        stale_tmp = fn.endswith(".tmp.png") and age > 3600
        expired = fn.endswith(".png") and not fn.endswith(".tmp.png") \
            and age > 14 * 86400
        if stale_tmp or expired:
            try:
                os.remove(path)
            except OSError:
                pass


def _ensure_worker():
    """Start the persistent background Blender once; reuse it thereafter."""
    global _pending_seq, _consumed_seq

    if _is_worker_alive():
        return True

    job_dir = _worker.get("job_dir") or tempfile.mkdtemp(prefix="node_peek_job_")
    cache_dir = _worker.get("cache_dir") or os.path.join(
        tempfile.gettempdir(), "node_peek_cache")
    os.makedirs(cache_dir, exist_ok=True)
    _prune_cache(cache_dir)
    # start clean so a stale response isn't mistaken for a fresh one
    try:
        os.remove(os.path.join(job_dir, "response.json"))
    except OSError:
        pass

    log_path = os.path.join(job_dir, "worker.log")
    args = [
        bpy.app.binary_path,
        "--background",
        "--factory-startup",
        "--python", WORKER_SCRIPT,
        "--",
        "--job", job_dir,
        "--cache", cache_dir,
        "--log", log_path,
    ]
    try:
        # requests go through stdin (the worker blocks on readline: zero idle
        # wake-ups, and it exits on EOF if we crash). stdout to DEVNULL:
        # Blender spams render progress; the worker logs to log_path instead.
        proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print("[Node Peek] failed to launch worker:", exc)
        return False

    _worker.update({"proc": proc, "job_dir": job_dir, "cache_dir": cache_dir})
    _pending_seq = -1
    _consumed_seq = -1
    print("[Node Peek] worker started, log:", log_path)
    return True


def _request_render(context):
    """Write the active material to a small lib and ask the worker to render.
    Cheap: only the material datablock is serialised, not the whole scene."""
    global _req_seq, _pending_seq, _last_material, _force_next, \
        _last_fingerprint, _progress_time, _last_path

    mat = _active_material(context)
    if mat is None:
        return False

    # which group (if any) is the user looking inside right now
    space, path = _find_shader_editor(mat)
    prefs = _prefs()
    fp = (_material_fingerprint(mat) + f"|r{prefs.resolution}|e{prefs.engine}"
          + "|p" + PATH_SEP.join(path))
    if not _force_next and fp == _last_fingerprint:
        # depsgraph fired but nothing preview-relevant changed (e.g. node
        # selection): skip the lib write and the worker wake-up entirely
        return True

    if not _ensure_worker():
        return False

    job_dir = _worker["job_dir"]
    lib_path = os.path.join(job_dir, "material.blend")
    try:
        # write just this material (+ dependencies). ABSOLUTE path remap keeps
        # relative image paths ("//textures/...") resolvable from the temp dir
        bpy.data.libraries.write(lib_path, {mat}, path_remap='ABSOLUTE',
                                 fake_user=True, compress=False)
    except Exception as exc:  # noqa: BLE001
        print("[Node Peek] libraries.write failed:", exc)
        return False

    # nodes the user is working on (in the tree they're viewing) render first
    priority = []
    view_tree = space.edit_tree if space is not None else mat.node_tree
    active = view_tree.nodes.active if view_tree else None
    if active is not None:
        priority.append(active.name)
    if view_tree:
        for n in view_tree.nodes:
            if len(priority) >= 8:
                break
            if n.select and n.name not in priority:
                priority.append(n.name)

    _req_seq += 1
    request = {
        "seq": _req_seq,
        "job": job_dir,
        "lib": lib_path,
        "material": mat.name,
        "res": prefs.resolution,
        "engine": prefs.engine,
        "force": _force_next,
        "priority": priority,
        "path": path,
    }
    proc = _worker["proc"]
    try:
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
    except Exception as exc:  # noqa: BLE001
        # broken pipe: the worker died between the alive check and the write.
        # Terminate it so the watchdog path relaunches cleanly.
        print("[Node Peek] could not send request:", exc)
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        return False

    _pending_seq = _req_seq
    _force_next = False
    _last_material = mat.name
    _last_fingerprint = fp
    _last_path = path
    _progress_time = time.time()
    return True


def _stop_worker():
    proc = _worker.get("proc")
    if _is_worker_alive():
        # stop request + EOF: readline returns immediately, worker exits
        try:
            proc.stdin.write(json.dumps({"stop": True}) + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        deadline = time.time() + 0.5
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.05)
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    _worker["proc"] = None


def _drop_all_previews(remove_datablocks):
    """Forget every texture. With remove_datablocks=False the Image datablock
    references are simply dropped (used after file load, when they are dead)."""
    if remove_datablocks:
        for img in _img_by_path.values():
            if img is None:
                continue
            try:
                bpy.data.images.remove(img)
            except Exception:  # noqa: BLE001
                pass
    _tex_by_path.clear()
    _img_by_path.clear()
    _node_pngpath.clear()


def _collect_results():
    """Read response.json and load any new pngs into GPU textures.

    The worker STREAMS: it rewrites the response after every render with the
    full node->png mapping, the list of pngs freshly rendered this job, and a
    ``done`` flag. Partial responses are applied as they come (the edited node
    pops in immediately); the request is only marked consumed on ``done``."""
    global _consumed_seq, _textures_material, _worker_failures, \
        _dirty, _last_edit_time, _last_fingerprint, _reload_seq, _progress_time

    job_dir = _worker.get("job_dir")
    cache_dir = _worker.get("cache_dir")
    if not job_dir or not cache_dir:
        return
    resp_path = os.path.join(job_dir, "response.json")
    if not os.path.exists(resp_path):
        return
    try:
        with open(resp_path) as fh:
            resp = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return

    seq = resp.get("seq", -1)
    done = bool(resp.get("done", True))
    if seq == _consumed_seq:
        # already applied (or discarded by load_post). Clean the file up, but
        # only when no job is in flight — the worker could be about to replace
        # it with the response we are waiting for.
        if _pending_seq == _consumed_seq:
            try:
                os.remove(resp_path)
            except OSError:
                pass
        return

    if _reload_seq != seq:  # new job: reset the per-job reload bookkeeping
        _reload_seq = seq
        _reloaded.clear()

    previews = resp.get("previews", {})
    fresh = set(resp.get("fresh", ()))

    new_tex = {}
    new_img = {}
    new_map = {}
    changed = False
    corrupt = False
    # keys are node names at top level, or "<group path>\x00<node name>" inside
    # a group; they are opaque strings here — only the draw callback decodes them
    for key, png_name in previews.items():
        png_path = os.path.join(cache_dir, png_name)
        # freshly (re)rendered this job -> must be (re)loaded from disk once,
        # even if we already had a texture for that same path (force refresh)
        needs_reload = png_name in fresh and png_name not in _reloaded
        if not needs_reload and png_path in new_tex:
            new_map[key] = png_path  # shared thumbnail (same hash)
            continue
        if not needs_reload and png_path in _tex_by_path:
            new_map[key] = png_path
            new_tex[png_path] = _tex_by_path[png_path]
            new_img[png_path] = _img_by_path.get(png_path)
            continue
        if not os.path.exists(png_path):
            # not rendered yet (streaming): keep the previous thumbnail so the
            # node doesn't flicker to blank while its update is in the queue
            old_path = _node_pngpath.get(key)
            if old_path and old_path in _tex_by_path:
                new_map[key] = old_path
                new_tex[old_path] = _tex_by_path[old_path]
                new_img[old_path] = _img_by_path.get(old_path)
            continue
        img = None
        try:
            img = bpy.data.images.load(png_path, check_existing=False)
            img.name = "." + png_name  # dot prefix hides it from UI lists
            img.colorspace_settings.name = 'sRGB'
            new_tex[png_path] = gpu.texture.from_image(img)
            new_img[png_path] = img
            new_map[key] = png_path
            _reloaded.add(png_name)
            changed = True
        except Exception as exc:  # noqa: BLE001
            print("[Node Peek] could not load", png_path, exc)
            corrupt = True
            if img is not None:  # loaded but texture creation failed
                try:
                    bpy.data.images.remove(img)
                except Exception:  # noqa: BLE001
                    pass
            # unreadable file would be a permanent cache hit: drop it so the
            # worker re-renders it on the next pass
            try:
                os.remove(png_path)
            except OSError:
                pass

    # free the Image datablocks whose texture was dropped or replaced
    for path, img in _img_by_path.items():
        if img is not None and new_img.get(path) is not img:
            try:
                bpy.data.images.remove(img)
            except Exception:  # noqa: BLE001
                pass

    _tex_by_path.clear()
    _tex_by_path.update(new_tex)
    _img_by_path.clear()
    _img_by_path.update(new_img)
    _node_pngpath.clear()
    _node_pngpath.update(new_map)
    _textures_material = resp.get("material") or _textures_material
    _progress_time = time.time()  # the job is making progress

    if done:
        _consumed_seq = seq
        _worker_failures = 0
        # consumed: delete the file so idle ticks stop re-parsing it forever
        try:
            os.remove(resp_path)
        except OSError:
            pass

    if corrupt:
        _last_fingerprint = None
        _dirty = True
        _last_edit_time = 0.0
    if changed or done:
        _tag_redraw_node_editors()


# ---------------------------------------------------------------------------
# Timer + handlers (live updates)
# ---------------------------------------------------------------------------

def _poll_timer():
    """Drives the debounced request dispatch and result collection. The worker
    stays alive between calls, so this only shuffles small json files."""
    global _dirty, _last_edit_time, _pending_seq, _worker_failures, \
        _last_fingerprint

    # always try to pick up a fresh response
    _collect_results()

    waiting = _pending_seq != _consumed_seq  # a request is still in flight

    # watchdog: recover if the worker died mid-job, OR if a job is stuck with
    # no streamed progress for too long (a genuine render hang — steady
    # progress keeps _progress_time fresh, so slow-but-advancing jobs are safe)
    dead = waiting and not _is_worker_alive()
    stuck = waiting and not dead and (time.time() - _progress_time) > _STUCK_TIMEOUT
    if dead or stuck:
        if stuck:
            print("[Node Peek] job stalled, restarting worker")
            _stop_worker()  # kill the hung process; next dispatch relaunches
        _worker_failures += 1
        _pending_seq = _consumed_seq
        _last_fingerprint = None  # make the retry actually dispatch
        waiting = False
        if _worker_failures < 3:
            if dead:
                print("[Node Peek] worker died, restarting"
                      f" (attempt {_worker_failures})")
            _dirty = True
            _last_edit_time = 0.0
        else:
            print("[Node Peek] worker keeps failing — check worker.log; "
                  "use Refresh Previews to retry")

    # auto-kick when the active material changes (incl. first sight) or when
    # the user steps into / out of a node group (so its interior gets rendered)
    if not _dirty and not waiting:
        mat = _active_material(bpy.context)
        if mat is not None:
            _, path = _find_shader_editor(mat)
            if mat.name != _last_material or path != _last_path:
                _dirty = True
                _last_edit_time = 0.0

    # debounced dispatch; don't pile requests while one is in flight
    if _dirty and not waiting:
        if _worker_failures >= 3 and not _force_next:
            _dirty = False  # circuit open: only a manual Refresh retries
        elif time.time() - _last_edit_time >= _prefs().debounce:
            _dirty = False
            _request_render(bpy.context)

    # fast tick while waiting for a render, relaxed tick when idle
    return 0.15 if (_pending_seq != _consumed_seq) else 0.3


@persistent  # without this, Blender clears the handler on File > Open
def _on_depsgraph_update(scene, depsgraph):
    global _dirty, _last_edit_time
    # only care if a material / node tree actually changed; the fingerprint
    # check at dispatch time filters out no-op events (selection etc.)
    for update in depsgraph.updates:
        id_ = update.id
        if isinstance(id_, (bpy.types.Material, bpy.types.ShaderNodeTree)):
            _dirty = True
            _last_edit_time = time.time()
            return


@persistent
def _on_load_post(_dummy):
    """A new file was loaded: every Image datablock we held is dead, and the
    previews belong to the previous file. Reset; the timer re-kicks naturally."""
    global _last_material, _dirty, _last_fingerprint, _consumed_seq, \
        _textures_material, _reload_seq, _last_path
    _drop_all_previews(remove_datablocks=False)  # refs are dangling, don't touch
    _enabled_nodes.clear()
    _reloaded.clear()
    _reload_seq = -1
    # mark any in-flight request as already consumed: its response belongs to
    # the PREVIOUS file and must be discarded, not applied, when it arrives
    _consumed_seq = _pending_seq
    _last_material = None
    _last_fingerprint = None
    _last_path = None
    _textures_material = None
    _dirty = False


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class NODEPEEK_OT_toggle_selected(bpy.types.Operator):
    bl_idname = "node.node_peek_toggle_selected"
    bl_label = "Toggle Node Peek"
    bl_description = "Toggle preview thumbnails for the selected nodes"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return (space and space.type == 'NODE_EDITOR'
                and space.tree_type == 'ShaderNodeTree'
                and space.edit_tree is not None)

    def execute(self, context):
        tree = context.space_data.edit_tree
        selected = [n for n in tree.nodes if n.select]
        if not selected:
            self.report({'WARNING'}, "No nodes selected")
            return {'CANCELLED'}
        for node in selected:
            key = f"{tree.name}/{node.name}"
            if key in _enabled_nodes:
                _enabled_nodes.discard(key)
            else:
                _enabled_nodes.add(key)
        global _dirty, _last_edit_time
        _dirty = True  # ensures textures exist if nothing rendered yet
        _last_edit_time = time.time()
        _tag_redraw_node_editors()
        return {'FINISHED'}


class NODEPEEK_OT_refresh(bpy.types.Operator):
    bl_idname = "node.node_peek_refresh"
    bl_label = "Refresh Previews"
    bl_description = "Force a re-render of all node previews, ignoring the cache"

    def execute(self, context):
        global _force_next, _dirty, _last_edit_time, _worker_failures
        if _active_material(context) is None:
            self.report({'WARNING'}, "Active object has no node-based material")
            return {'CANCELLED'}
        _worker_failures = 0  # manual retry resets the circuit breaker
        _force_next = True
        _dirty = True
        _last_edit_time = 0.0
        self.report({'INFO'}, "Re-rendering previews...")
        return {'FINISHED'}


class NODEPEEK_OT_clear(bpy.types.Operator):
    bl_idname = "node.node_peek_clear"
    bl_label = "Clear Previews"
    bl_description = ("Remove all thumbnails and empty the render cache. "
                      "Previews rebuild on the next edit or Refresh")

    def execute(self, context):
        global _last_fingerprint
        _drop_all_previews(remove_datablocks=True)
        cache_dir = _worker.get("cache_dir")
        if cache_dir and os.path.isdir(cache_dir):
            for fn in os.listdir(cache_dir):
                if fn.endswith(".png"):
                    try:
                        os.remove(os.path.join(cache_dir, fn))
                    except OSError:
                        pass
        _last_fingerprint = None  # next edit re-dispatches even if unchanged
        _tag_redraw_node_editors()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI panel
# ---------------------------------------------------------------------------

class NODEPEEK_PT_panel(bpy.types.Panel):
    bl_label = "Node Peek"
    bl_idname = "NODE_PT_node_peek"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Node Peek"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space and space.tree_type == 'ShaderNodeTree'

    def draw(self, context):
        layout = self.layout
        prefs = _prefs()
        layout.prop(prefs, "visible_by_default")
        layout.prop(prefs, "resolution")
        layout.separator()
        layout.operator("node.node_peek_refresh", icon='FILE_REFRESH')
        layout.operator("node.node_peek_toggle_selected", icon='HIDE_OFF')
        layout.operator("node.node_peek_clear", icon='TRASH')
        if _worker_failures >= 3:
            layout.label(text="Worker keeps crashing — see worker.log",
                         icon='ERROR')
        elif _pending_seq != _consumed_seq:
            layout.label(text="Rendering...", icon='SORTTIME')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    NODEPEEK_Preferences,
    NODEPEEK_OT_toggle_selected,
    NODEPEEK_OT_refresh,
    NODEPEEK_OT_clear,
    NODEPEEK_PT_panel,
)

_keymaps = []


def register():
    global _draw_handle
    for cls in _classes:
        bpy.utils.register_class(cls)

    _draw_handle = bpy.types.SpaceNodeEditor.draw_handler_add(
        _draw_callback, (), 'WINDOW', 'POST_PIXEL')

    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)

    if not bpy.app.timers.is_registered(_poll_timer):
        bpy.app.timers.register(_poll_timer, first_interval=1.0, persistent=True)

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Node Editor", space_type='NODE_EDITOR')
        kmi = km.keymap_items.new(
            "node.node_peek_toggle_selected", 'P', 'PRESS', ctrl=True, shift=True)
        _keymaps.append((km, kmi))


def unregister():
    global _draw_handle

    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()

    if bpy.app.timers.is_registered(_poll_timer):
        bpy.app.timers.unregister(_poll_timer)

    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)

    if _draw_handle is not None:
        bpy.types.SpaceNodeEditor.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None

    _stop_worker()
    job_dir = _worker.get("job_dir")
    if job_dir:
        shutil.rmtree(job_dir, ignore_errors=True)
        _worker["job_dir"] = None
    _drop_all_previews(remove_datablocks=True)

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
