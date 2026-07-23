# SPDX-License-Identifier: GPL-3.0-or-later
# Node Peek — live rendered thumbnails above shader nodes.
# Copyright (C) 2026 Aurélien — Résidence Principale (mlstr0m).
"""
Persistent background worker for Node Peek.

Launched once as:

    blender --background --factory-startup --python worker.py -- \
        --job JOB_DIR --cache CACHE_DIR --log LOG

Requests arrive as single JSON lines on **stdin** (zero idle wake-ups: the
worker blocks on readline, and gets EOF if the parent Blender dies for any
reason). Responses are written to ``JOB_DIR/response.json``.

For each job the worker renders only the nodes whose content hash is not
already cached, and **streams** a partial response after every render, so the
add-on can display thumbnails as they complete — the edited node comes first.

Key performance choices:
  * ONE long-lived Blender process -> no per-edit startup cost.
  * Per-node content hashing (node-group internals, image file mtimes) ->
    unchanged nodes are never re-rendered.
  * Image datablocks are cached across jobs (path+mtime+colorspace key), so
    big textures are loaded once instead of on every edit.
  * Flat maps render at 1 Cycles sample (pure emission, noise-free); spheres
    use adaptive sampling + denoise.
  * Renders go to a temp file then os.replace() into the cache, so a killed
    worker can never leave a corrupt PNG poisoning the cache.
  * Limited render threads, low process priority.
"""

import os
import sys
import json
import math
import hashlib
import argparse

import bpy


# Bump whenever the preview scene (lighting, camera, world, geometry) changes,
# so previously cached PNGs are invalidated automatically.
SCENE_VERSION = 3

_LOG_PATH = None

# cross-job image datablock cache: (abspath, mtime, colorspace, alpha) -> Image
_image_cache = {}
_IMAGE_CACHE_MAX = 16

# add-on custom node types we registered a stub class for (see ensure_stub_types)
_stub_types = set()
_stub_failed = set()


def log(*msg):
    line = " ".join(str(m) for m in msg)
    if _LOG_PATH:
        try:
            # explicit utf-8: the platform default (cp1252 on Windows) would
            # raise UnicodeEncodeError on non-ASCII node/material names — and
            # log() runs inside error handlers, where raising kills the worker
            with open(_LOG_PATH, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    else:
        print(line)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--job", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--log", default="")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Render setup
# ---------------------------------------------------------------------------

def setup_render(scene, res, engine):
    # EEVEE renders black without a GPU context in --background, so previews use
    # Cycles, which is reliable headless. "EEVEE" is an explicit opt-in only.
    if engine == "EEVEE":
        for name in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
            try:
                scene.render.engine = name
                break
            except (TypeError, ValueError):
                continue
    else:
        scene.render.engine = "CYCLES"
        try:
            scene.cycles.device = "CPU"  # tiny frames: CPU avoids GPU spin-up
        except Exception:
            pass

    scene.render.resolution_x = res
    scene.render.resolution_y = res
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.compression = 15
    try:
        scene.view_settings.view_transform = "Standard"
    except (TypeError, ValueError):
        pass  # custom OCIO config without "Standard": use its default view

    # NOTE: use_persistent_data is deliberately OFF. We rewire the surface link
    # between every node render inside a job; if Cycles failed to re-sync that
    # change, one node could bake another node's result into the cache — a
    # silent, sticky corruption. The image datablock cache below gives the big
    # texture win without that risk.

    # keep the machine responsive and cool: don't grab every core
    try:
        scene.render.threads_mode = "FIXED"
        scene.render.threads = max(1, min(4, (os.cpu_count() or 4) // 2))
    except Exception:
        pass


def set_samples(scene, flat):
    if scene.render.engine != "CYCLES":
        return
    if flat:
        # pure emission: one sample is exact, no denoise needed
        scene.cycles.use_adaptive_sampling = False
        scene.cycles.samples = 1
        scene.cycles.use_denoising = False
    else:
        scene.cycles.use_adaptive_sampling = True
        scene.cycles.adaptive_threshold = 0.1
        scene.cycles.samples = 16  # denoised: plenty at thumbnail size
        scene.cycles.use_denoising = True
    scene.cycles.seed = 0  # deterministic -> cache stays valid


# ---------------------------------------------------------------------------
# Preview scene (built once, reused for every job)
# ---------------------------------------------------------------------------

class PreviewScene:
    def __init__(self, scene, plane, sphere, ortho_cam, persp_cam,
                 composite, direct_output, normalized_output):
        self.scene = scene
        self.plane = plane
        self.sphere = sphere
        self.ortho_cam = ortho_cam
        self.persp_cam = persp_cam
        self.composite = composite
        self.direct_output = direct_output
        self.normalized_output = normalized_output

    def use_flat(self):
        self.plane.hide_render = False
        self.sphere.hide_render = True
        self.scene.camera = self.ortho_cam

    def use_sphere(self):
        self.plane.hide_render = True
        self.sphere.hide_render = False
        self.scene.camera = self.persp_cam

    def assign_material(self, material):
        for obj in (self.plane, self.sphere):
            obj.data.materials.clear()
            obj.data.materials.append(material)

    def set_normalize_data(self, enabled):
        """Select the compositor output used for this one preview render."""
        tree = self.composite.id_data
        for link in list(self.composite.inputs["Image"].links):
            tree.links.remove(link)
        tree.links.new(
            self.normalized_output if enabled else self.direct_output,
            self.composite.inputs["Image"])


def build_preview_scene():
    import bmesh

    scene = bpy.data.scenes.new("_np_preview")

    # flat plane (-1..1) that exactly fills the orthographic camera
    plane_mesh = bpy.data.meshes.new("_np_plane")
    bm = bmesh.new()
    verts = [bm.verts.new(v) for v in
             ((-1, 0, -1), (1, 0, -1), (1, 0, 1), (-1, 0, 1))]
    face = bm.faces.new(verts)
    uv = bm.loops.layers.uv.new("UVMap")
    for loop, co in zip(face.loops, ((0, 0), (1, 0), (1, 1), (0, 1))):
        loop[uv].uv = co
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(plane_mesh)
    bm.free()
    plane = bpy.data.objects.new("_np_plane", plane_mesh)
    scene.collection.objects.link(plane)

    # shaded sphere for shader outputs
    sphere_mesh = bpy.data.meshes.new("_np_sphere")
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=1.0)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    # spherical UVs: most PBR materials sample textures in UV space, so without
    # a UV layer the sphere would sample one texel -> flat colour, hiding the
    # normal map / colour pattern. A lat-long unwrap is plenty for a thumbnail.
    uv = bm.loops.layers.uv.verify()
    for face in bm.faces:
        for loop in face.loops:
            co = loop.vert.co
            loop[uv].uv = (
                0.5 + math.atan2(co.y, co.x) / (2.0 * math.pi),
                0.5 + math.asin(max(-1.0, min(1.0, co.z))) / math.pi,
            )
    bm.to_mesh(sphere_mesh)
    bm.free()
    for poly in sphere_mesh.polygons:
        poly.use_smooth = True
    sphere = bpy.data.objects.new("_np_sphere", sphere_mesh)
    scene.collection.objects.link(sphere)

    ortho_data = bpy.data.cameras.new("_np_ortho")
    ortho_data.type = "ORTHO"
    ortho_data.ortho_scale = 2.0
    ortho_cam = bpy.data.objects.new("_np_ortho", ortho_data)
    ortho_cam.location = (0.0, -5.0, 0.0)
    ortho_cam.rotation_euler = (1.5708, 0.0, 0.0)
    scene.collection.objects.link(ortho_cam)

    persp_data = bpy.data.cameras.new("_np_persp")
    persp_cam = bpy.data.objects.new("_np_persp", persp_data)
    persp_cam.location = (0.0, -3.2, 0.0)
    persp_cam.rotation_euler = (1.5708, 0.0, 0.0)
    scene.collection.objects.link(persp_cam)

    light_data = bpy.data.lights.new("_np_light", type="AREA")
    light_data.energy = 400.0
    light_data.size = 5.0
    light = bpy.data.objects.new("_np_light", light_data)
    light.location = (2.5, -2.5, 3.0)
    light.rotation_euler = (0.6, 0.2, 0.6)
    scene.collection.objects.link(light)

    # a light neutral-grey environment: opaque materials read well and a
    # transparent surface shows the backdrop through it instead of pure black
    world = bpy.data.worlds.new("_np_world")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.18, 0.18, 0.18, 1.0)
        bg.inputs[1].default_value = 1.0
    scene.world = world

    # The render result remains float until Blender writes the PNG.  Keep this
    # compositor graph alive for the worker lifetime and only switch its final
    # link per preview.  Normalize is scalar-only, hence the RGB split.
    scene.use_nodes = True
    # Blender 5.2 moved compositor trees from Scene.node_tree to an explicit
    # compositor node group, and replaced Composite with Group Output. Keep
    # both layouts working: the extension supports Blender 4.2 and newer.
    comp = getattr(scene, "node_tree", None)
    modern_compositor = comp is None
    if modern_compositor:
        comp = bpy.data.node_groups.new("_np_compositor", "CompositorNodeTree")
        scene.compositing_node_group = comp
    comp.nodes.clear()
    layers = comp.nodes.new("CompositorNodeRLayers")
    # A Render Layers node defaults to Blender's startup "Scene" when it lives
    # in the standalone compositor group introduced in Blender 5.2. Point it
    # at our private preview scene explicitly, or every thumbnail contains the
    # default cube instead of Node Peek's plane/sphere.
    layers.scene = scene
    if modern_compositor:
        comp.interface.new_socket(name="Image", in_out='OUTPUT',
                                  socket_type="NodeSocketColor")
        output = comp.nodes.new("NodeGroupOutput")
        separate = comp.nodes.new("CompositorNodeSeparateColor")
        combine = comp.nodes.new("CompositorNodeCombineColor")
        channels = (("Red", "Green", "Blue"), "Alpha")
    else:
        output = comp.nodes.new("CompositorNodeComposite")
        separate = comp.nodes.new("CompositorNodeSepRGBA")
        combine = comp.nodes.new("CompositorNodeCombRGBA")
        channels = (("R", "G", "B"), "A")
    normalizers = [comp.nodes.new("CompositorNodeNormalize") for _ in range(3)]
    comp.links.new(layers.outputs["Image"], separate.inputs["Image"])
    rgb_channels, alpha_channel = channels
    for channel, normalizer in zip(rgb_channels, normalizers):
        comp.links.new(separate.outputs[channel], normalizer.inputs[0])
        comp.links.new(normalizer.outputs[0], combine.inputs[channel])
    comp.links.new(separate.outputs[alpha_channel], combine.inputs[alpha_channel])
    # Start with the unmodified output. render_socket selects the appropriate
    # branch before every render, including shader previews.
    comp.links.new(layers.outputs["Image"], output.inputs["Image"])

    return PreviewScene(scene, plane, sphere, ortho_cam, persp_cam, output,
                        layers.outputs["Image"], combine.outputs["Image"])


# ---------------------------------------------------------------------------
# Node helpers + content hashing
# ---------------------------------------------------------------------------

def find_output_node(tree):
    for node in tree.nodes:
        if node.type == "OUTPUT_MATERIAL" and node.is_active_output:
            return node
    for node in tree.nodes:
        if node.type == "OUTPUT_MATERIAL":
            return node
    return None


def pick_output_socket(node):
    candidates = [s for s in node.outputs if s.enabled and not s.hide]
    if not candidates:
        return None
    for s in candidates:
        if s.is_linked:
            return s
    return candidates[0]


def previewable(node):
    if node.bl_idname == "NodeUndefined":
        # an add-on node type we couldn't stub (pure-Python node, other render
        # engine...): it evaluates to nothing in Cycles — here AND in the
        # user's own render. No thumbnail is honest; a flat one would mislead.
        return False
    if node.type in {"FRAME", "REROUTE", "GROUP_INPUT", "GROUP_OUTPUT"}:
        return False
    if node.type in {"OUTPUT_MATERIAL", "OUTPUT_WORLD", "OUTPUT_LIGHT"}:
        return False
    return bool(node.outputs)


# Purely cosmetic properties must NOT be part of the hash, or recoloring a
# node header would needlessly re-render it.
# NOTE: keep in sync with _FP_SKIP_PROPS in __init__.py.
_HASH_SKIP_PROPS = {"rna_type", "name", "label", "location", "width",
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


def _image_fingerprint(image):
    """Identify an image by name, path AND file mtime, so editing the file on
    disk (or repacking) invalidates cached previews."""
    parts = [f"img:{image.name}:{image.source}:{image.filepath}"]
    try:
        path = bpy.path.abspath(image.filepath, library=image.library)
        parts.append(str(os.path.getmtime(path)))
    except (OSError, TypeError, ValueError):
        pass
    packed = getattr(image, "packed_file", None)
    if packed is not None:
        parts.append(f"packed:{packed.size}")
    return ":".join(parts)


def group_signature(subtree):
    """Hash of a node group's *contents*: the chain feeding its output node.
    Nodes not connected to the group output are correctly ignored."""
    out = None
    for n in subtree.nodes:
        if n.type == "GROUP_OUTPUT" and n.is_active_output:
            out = n
            break
    if out is None:
        for n in subtree.nodes:
            if n.type == "GROUP_OUTPUT":
                out = n
                break
    if out is None:
        return "noout"
    cache = {}
    parts = []
    for inp in out.inputs:
        if inp.is_linked:
            link = inp.links[0]
            parts.append(node_signature(link.from_node, cache)
                         + ":" + link.from_socket.identifier)
        else:
            parts.append(repr(_socket_value(inp)))
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:16]


def node_signature(node, sig_cache):
    """Content hash of a node *and everything feeding into it*. Two nodes with
    identical upstream graphs hash the same, so their preview is reused."""
    cached = sig_cache.get(node.name)
    if cached is not None:
        return cached
    sig_cache[node.name] = ""  # guard against pathological cycles

    parts = [node.bl_idname, str(node.mute)]

    # generic scalar/enum/string properties
    for prop in node.bl_rna.properties:
        pid = prop.identifier
        if pid in _HASH_SKIP_PROPS:
            continue
        if prop.type in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"} and not prop.is_readonly:
            try:
                val = getattr(node, pid)
                if getattr(prop, "is_array", False):
                    val = tuple(val)
                parts.append(f"{pid}={val}")
            except Exception:
                pass

    # colour ramps
    ramp = getattr(node, "color_ramp", None)
    if ramp is not None:
        parts.append(f"ramp:{ramp.color_mode}:{ramp.interpolation}")
        for e in ramp.elements:
            parts.append(f"{round(e.position, 5)}:{tuple(round(c, 5) for c in e.color)}")

    # curve mappings (RGB / vector / float curves)
    mapping = getattr(node, "mapping", None)
    if mapping is not None and hasattr(mapping, "curves"):
        for curve in mapping.curves:
            for pt in curve.points:
                parts.append(f"{tuple(round(x, 5) for x in pt.location)}:{pt.handle_type}")

    # image datablocks (identity + file mtime)
    image = getattr(node, "image", None)
    if image is not None:
        parts.append(_image_fingerprint(image))

    # node groups: hash the group's INTERNAL tree too, or edits inside a group
    # would leave stale previews for the group node and everything downstream
    subtree = getattr(node, "node_tree", None)
    if subtree is not None:
        parts.append("G:" + group_signature(subtree))

    # inputs: recurse upstream when linked, else hash the value
    for inp in node.inputs:
        if inp.is_linked:
            link = inp.links[0]
            parts.append("L:" + node_signature(link.from_node, sig_cache)
                         + ":" + link.from_socket.identifier)
        else:
            parts.append("D:" + repr(_socket_value(inp)))

    digest = hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:16]
    sig_cache[node.name] = digest
    return digest


# ---------------------------------------------------------------------------
# Material (re)loading + image datablock cache
# ---------------------------------------------------------------------------

def ensure_stub_types(idnames):
    """Register empty ShaderNodeCustomGroup stubs for add-on node types that
    don't exist in this --factory-startup process.

    Blender binds nodes to their type by bl_idname at load time. Without the
    add-on, custom nodes load as NodeUndefined: their internal tree becomes
    unreachable and everything downstream evaluates flat. A do-nothing stub
    with the right bl_idname makes the node a functional custom group again —
    its internal tree is already in the lib (written as a dependency), and
    Cycles renders custom groups natively, so previews match what the user
    sees in their own render.

    idnames are plain strings taken from the user's material; no code from
    the file is executed — the class body is entirely ours."""
    for idname in idnames:
        if not idname or idname in _stub_types or idname in _stub_failed:
            continue
        if hasattr(bpy.types, idname):
            _stub_types.add(idname)  # already a real registered type
            continue
        try:
            cls = type(f"NodePeekStub{len(_stub_types)}",
                       (bpy.types.ShaderNodeCustomGroup,),
                       {"bl_idname": idname, "bl_label": idname})
            bpy.utils.register_class(cls)
            _stub_types.add(idname)
            log("registered stub node type:", idname)
        except Exception as exc:  # noqa: BLE001
            # this type keeps today's behaviour (skipped, see previewable());
            # never worse than before the feature existed
            _stub_failed.add(idname)
            log("stub registration failed:", idname, exc)


def load_material(lib_path, material_name):
    before = set(bpy.data.materials.keys())
    try:
        with bpy.data.libraries.load(lib_path, link=False) as (src, dst):
            if material_name in src.materials:
                dst.materials = [material_name]
    except Exception as exc:  # noqa: BLE001
        log("load_material failed:", exc)
        return None
    new = [bpy.data.materials[n] for n in bpy.data.materials.keys() if n not in before]
    if new:
        return new[0]
    return bpy.data.materials.get(material_name)


def adopt_cached_images(material):
    """Swap freshly-loaded image datablocks for cached ones (same file, same
    mtime, same colorspace), so a big texture is decoded from disk once and
    reused across jobs instead of on every edit. Packed images are skipped
    (no safe identity key)."""
    imgs = {}
    seen_trees = set()

    def walk(tree):
        if tree is None or tree.name_full in seen_trees:
            return
        seen_trees.add(tree.name_full)
        for node in tree.nodes:
            img = getattr(node, "image", None)
            if img is not None:
                imgs[img.name_full] = img
            walk(getattr(node, "node_tree", None))

    walk(material.node_tree)

    for img in imgs.values():
        if getattr(img, "packed_file", None) is not None or not img.filepath:
            continue
        try:
            path = bpy.path.abspath(img.filepath)
            mtime = os.path.getmtime(path)
        except (OSError, TypeError, ValueError):
            continue
        key = (path, round(mtime, 3),
               img.colorspace_settings.name, img.alpha_mode)
        cached = _image_cache.get(key)
        if cached is not None:
            try:
                cached.name  # validity probe: raises if the ID was freed
            except ReferenceError:
                cached = None
                _image_cache.pop(key, None)
        if cached is not None and cached != img:
            try:
                img.user_remap(cached)
                bpy.data.images.remove(img)
            except Exception as exc:  # noqa: BLE001
                log("image remap failed:", exc)
        elif cached is None:
            # fake_user protects the datablock from purge_material()
            img.use_fake_user = True
            _image_cache[key] = img
            while len(_image_cache) > _IMAGE_CACHE_MAX:
                old_key = next(iter(_image_cache))
                if old_key == key:
                    break  # never evict the entry we just added
                old = _image_cache.pop(old_key)
                try:
                    old.use_fake_user = False
                    bpy.data.images.remove(old)
                except Exception:  # noqa: BLE001
                    pass


def purge_material(material):
    try:
        bpy.data.materials.remove(material)
    except Exception:
        pass
    # drop the now-orphaned node trees / images the material pulled in
    # (cached images survive: they carry a fake user)
    for _ in range(4):
        try:
            n = bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True,
                                       do_recursive=True)
        except TypeError:
            n = bpy.data.orphans_purge()
        except Exception:
            break
        if not n:
            break


# ---------------------------------------------------------------------------
# Job processing
# ---------------------------------------------------------------------------

def write_response(job_dir, seq, material_name, previews, fresh, done):
    tmp = os.path.join(job_dir, "response.json.tmp")
    with open(tmp, "w") as fh:
        json.dump({"seq": seq, "material": material_name, "done": done,
                   "previews": previews, "fresh": fresh}, fh)
    os.replace(tmp, os.path.join(job_dir, "response.json"))


# separates the group-instance path from the node name in a response key;
# NUL can't appear in a Blender ID name, so it's collision-proof.
# NOTE: keep in sync with PATH_SEP in __init__.py.
PATH_SEP = "\x00"


def active_group_output(tree):
    fallback = None
    for n in tree.nodes:
        if n.type == "GROUP_OUTPUT":
            if n.is_active_output:
                return n
            fallback = fallback or n
    return fallback


def _tear_down(items):
    for tree, item in reversed(items):
        try:
            tree.interface.remove(item)
        except Exception:  # noqa: BLE001
            pass


def bubble_socket(chain, target_socket, is_shader):
    """Route ``target_socket`` (a socket inside the innermost group's tree) all
    the way up to the material tree by adding a temporary interface OUTPUT
    socket at each nesting level and wiring it through the group instances.

    Returns (material_tree_socket, items). ``items`` must be passed to
    _tear_down() after rendering to remove every temporary socket (which also
    drops the links and the propagated instance sockets). Returns (None, [])
    if anything unexpected is encountered — the caller then skips that node."""
    socktype = "NodeSocketShader" if is_shader else "NodeSocketColor"
    items = []
    source = target_socket
    for group_node in reversed(chain):
        subtree = group_node.node_tree
        go = active_group_output(subtree)
        if go is None:
            _tear_down(items)
            return None, []
        try:
            item = subtree.interface.new_socket(
                "__np_out", in_out="OUTPUT", socket_type=socktype)
        except Exception:  # noqa: BLE001
            _tear_down(items)
            return None, []
        items.append((subtree, item))
        go_in = go.inputs.get("__np_out")
        if go_in is None:
            _tear_down(items)
            return None, []
        try:
            subtree.links.new(source, go_in)
        except Exception:  # noqa: BLE001
            _tear_down(items)
            return None, []
        # the group instance node gains a matching output socket
        ns = group_node.outputs.get("__np_out")
        if ns is None and len(group_node.outputs):
            ns = group_node.outputs[-1]  # not yet name-indexed: take the newest
        # verify we really got OUR socket: custom group nodes bound to a stub
        # don't auto-sync instance sockets with interface changes, and the
        # fallback would silently pick a DIFFERENT socket — a wrong thumbnail
        # is worse than none.
        if ns is None or ns.name != "__np_out":
            _tear_down(items)
            return None, []
        source = ns
    return source, items


def render_socket(preview, surface_in, emission, source, is_shader,
                  tmp_path, png_path, normalize_data=False):
    """Wire ``source`` (a socket in the material tree) into the preview output,
    render, and atomically move the result into the cache."""
    mt = surface_in.id_data
    for link in list(surface_in.links):
        mt.links.remove(link)
    if is_shader:
        mt.links.new(source, surface_in)
        preview.use_sphere()
    else:
        for link in list(emission.inputs["Color"].links):
            mt.links.remove(link)
        mt.links.new(source, emission.inputs["Color"])
        mt.links.new(emission.outputs["Emission"], surface_in)
        preview.use_flat()
    # Shader previews depict a lit material, not raw node data. Normalizing
    # their final image would distort lighting, reflections and the backdrop.
    preview.set_normalize_data(normalize_data and not is_shader)
    set_samples(preview.scene, not is_shader)
    preview.scene.render.filepath = tmp_path
    bpy.ops.render.render(write_still=True, scene=preview.scene.name)
    os.replace(tmp_path, png_path)


def resolve_chain(material_tree, path):
    """Follow a list of group-node names from the material tree inward.
    Returns (chain, target_tree) or (None, None) if the path no longer maps."""
    chain = []
    tree = material_tree
    for gname in path:
        gn = tree.nodes.get(gname)
        if gn is None or getattr(gn, "node_tree", None) is None:
            return None, None
        chain.append(gn)
        tree = gn.node_tree
    return chain, tree


def process(preview, req, cache_dir):
    seq = req["seq"]
    job_dir = req["job"]
    mat_name = req["material"]
    res = int(req.get("res", 150))
    engine = req.get("engine", "CYCLES")
    normalize_data = bool(req.get("normalize_data_previews", False))
    force = bool(req.get("force", False))
    priority = req.get("priority", [])

    setup_render(preview.scene, res, engine)

    # bind add-on custom node types BEFORE loading, or they come in undefined
    ensure_stub_types(req.get("custom_types") or [])

    material = load_material(req["lib"], mat_name)
    if material is None or material.node_tree is None:
        write_response(job_dir, seq, mat_name, {}, [], True)
        return
    adopt_cached_images(material)
    preview.assign_material(material)

    tree = material.node_tree
    output_node = find_output_node(tree)
    if output_node is None:
        purge_material(material)
        write_response(job_dir, seq, mat_name, {}, [], True)
        return

    surface_in = output_node.inputs["Surface"]
    # snapshot the node list BEFORE adding the emission helper, so the helper
    # is never previewed itself
    top_nodes = [n for n in tree.nodes if previewable(n)]
    emission = tree.nodes.new("ShaderNodeEmission")

    sig_cache = {}
    # res/engine and the preview-scene version are part of the on-disk cache
    # key, so changing any of them transparently re-renders.
    salt = f"|r{res}|e{engine}|s{SCENE_VERSION}"

    # a "target" = one thumbnail to produce:
    #   (key, prio_name, is_shader, png_name, chain, node, socket)
    # chain is None at top level, or the group-instance stack for nodes inside
    # a group (used to route the socket out to the material output).
    targets = []
    for node in top_nodes:
        socket = pick_output_socket(node)
        if socket is None:
            continue
        target_salt = salt + ("|n1" if normalize_data and socket.type != "SHADER"
                             else "")
        digest = hashlib.md5(
            (node_signature(node, sig_cache) + target_salt).encode()).hexdigest()[:16]
        targets.append((node.name, node.name, socket.type == "SHADER",
                        f"{digest}.png", None, node, socket))

    # nodes INSIDE the group the user is currently viewing (if any)
    path = req.get("path") or []
    if path:
        chain, target_tree = resolve_chain(tree, path)
        if chain and target_tree is not None:
            pathkey = PATH_SEP.join(path)
            instance_sig = "/".join(node_signature(gn, {}) for gn in chain)
            tcache = {}
            for node in target_tree.nodes:
                if not previewable(node):
                    continue
                socket = pick_output_socket(node)
                if socket is None:
                    continue
                target_salt = salt + ("|n1" if normalize_data
                                     and socket.type != "SHADER" else "")
                digest = hashlib.md5(
                    (node_signature(node, tcache) + "|I:" + instance_sig
                     + target_salt).encode()).hexdigest()[:16]
                key = pathkey + PATH_SEP + node.name
                targets.append((key, node.name, socket.type == "SHADER",
                                f"{digest}.png", chain, node, socket))

    # build the response map (all targets) and the render queue (misses only)
    previews = {}
    todo = []
    seen_pngs = set()
    for tgt in targets:
        key, _prio_name, _is_shader, png_name, _chain, _node, _socket = tgt
        previews[key] = png_name
        if not force and os.path.exists(os.path.join(cache_dir, png_name)):
            continue
        if png_name in seen_pngs:
            continue
        seen_pngs.add(png_name)
        todo.append(tgt)

    # render the node(s) the user is editing first, then the cheap flat maps
    prio = {name: i for i, name in enumerate(priority)}
    todo.sort(key=lambda t: (prio.get(t[1], len(prio)), t[2]))

    fresh = []
    write_response(job_dir, seq, mat_name, previews, fresh, done=not todo)
    if not todo:
        purge_material(material)
        log(f"[job {seq}] 0 rendered, {len(previews)} cached")
        return

    rendered = 0
    for key, _prio_name, is_shader, png_name, chain, node, socket in todo:
        png_path = os.path.join(cache_dir, png_name)
        tmp_path = os.path.join(
            cache_dir, f"{png_name[:-4]}.{os.getpid()}.tmp.png")
        items = []
        try:
            if chain is None:
                source = socket
            else:
                # route the in-group socket out to the material tree; safe to
                # mutate the group interface here — this is a throwaway copy
                source, items = bubble_socket(chain, socket, is_shader)
                if source is None:
                    previews.pop(key, None)
                    continue
            render_socket(preview, surface_in, emission, source, is_shader,
                          tmp_path, png_path, normalize_data)
            rendered += 1
            fresh.append(png_name)
            # stream: let the add-on display this thumbnail right away
            write_response(job_dir, seq, mat_name, previews, fresh, done=False)
        except Exception as exc:  # noqa: BLE001
            previews.pop(key, None)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            log("render failed", key.replace(PATH_SEP, "/"), exc)
        finally:
            _tear_down(items)  # remove any temporary group interface sockets

    purge_material(material)
    write_response(job_dir, seq, mat_name, previews, fresh, done=True)
    log(f"[job {seq}] {rendered} rendered, {len(previews) - rendered} cached")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    global _LOG_PATH
    args = parse_args()
    if args.log:
        _LOG_PATH = args.log
        try:  # start each session with a fresh log
            open(_LOG_PATH, "w").close()
        except OSError:
            _LOG_PATH = None
    os.makedirs(args.cache, exist_ok=True)

    # be a good background citizen: lowest-impact scheduling
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass

    preview = build_preview_scene()
    last_seq = -1
    log("[Node Peek] worker ready")

    # blocking readline: zero CPU while idle. EOF (empty string) means the
    # parent Blender closed the pipe — cleanly or by crashing — so we exit.
    while True:
        line = sys.stdin.readline()
        if not line:
            log("[Node Peek] stdin closed, exiting")
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            log("[Node Peek] bad request line")
            continue
        if req.get("stop"):
            log("[Node Peek] stop requested, exiting")
            break
        if req.get("seq", -1) == last_seq:
            continue
        last_seq = req.get("seq", -1)
        try:
            process(preview, req, args.cache)
        except Exception as exc:  # noqa: BLE001
            log("[Node Peek] job failed:", exc)
            # ALWAYS answer, or the add-on would wait forever on this seq
            try:
                write_response(req.get("job", args.job), req.get("seq", -1),
                               req.get("material", ""), {}, [], True)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    main()
