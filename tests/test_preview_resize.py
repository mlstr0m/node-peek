"""Blender-side regression test for per-node preview sizing.

Run:
    blender --background --factory-startup --python test_preview_resize.py
"""
import importlib.util
import os
import shutil
import tempfile
from types import SimpleNamespace

import bpy


repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "node_peek_resize_test", os.path.join(repo, "__init__.py"))
node_peek = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node_peek)


def check(condition, label):
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        raise AssertionError(label)


node_peek.register()
test_dir = tempfile.mkdtemp(prefix="node_peek_resize_test_")
try:
    check(not bpy.data.is_dirty, "registration does not dirty the blend")
    bpy.context.window_manager.node_peek_preview_scale = 50.0
    check(not bpy.data.is_dirty,
          "size property without a node context does not dirty the blend")

    mesh = bpy.data.meshes.new("ResizeTestMesh")
    obj = bpy.data.objects.new("ResizeTestObject", mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    mat = bpy.data.materials.new("ResizeTestMaterial")
    mat.use_nodes = True
    obj.data.materials.append(mat)
    tree = mat.node_tree
    first = tree.nodes.new("ShaderNodeTexNoise")
    first.name = "First"
    second = tree.nodes.new("ShaderNodeTexVoronoi")
    second.name = "Second"

    for node in tree.nodes:
        node.select = False
    first.select = True
    second.select = True
    tree.nodes.active = first
    bpy.ops.wm.save_as_mainfile(
        filepath=os.path.join(test_dir, "resize_test.blend"))
    check(not bpy.data.is_dirty, "test blend starts clean")

    space = SimpleNamespace(
        type='NODE_EDITOR',
        tree_type='ShaderNodeTree',
        edit_tree=tree,
        id=mat,
        path=[],
    )
    context = SimpleNamespace(space_data=space)
    selected = node_peek._selected_preview_nodes(context)
    check({node.name for node in selected} == {"First", "Second"},
          "both selected previewable nodes are targeted")

    node_peek._set_selected_preview_scale(context, 50.0)
    check(not bpy.data.is_dirty,
          "resizing selected previews does not dirty the blend")
    first_key = node_peek._context_scale_key(context, first)
    second_key = node_peek._context_scale_key(context, second)
    check(node_peek._preview_scale_by_key[first_key] == 0.5,
          "slider resizes first selected node")
    check(node_peek._preview_scale_by_key[second_key] == 0.5,
          "slider resizes second selected node")

    second.select = False
    tree.nodes.active = first
    node_peek._set_selected_preview_scale(context, 175.0)
    check(node_peek._preview_scale_by_key[first_key] == 1.75,
          "single selection can be resized independently")
    check(node_peek._preview_scale_by_key[second_key] == 0.5,
          "unselected node keeps its own size")
    check(node_peek._selected_preview_scale(context) == 175.0,
          "slider reads the active node size")

    node_peek._reset_selected_preview_scales(context)
    check(first_key not in node_peek._preview_scale_by_key,
          "reset selected restores only the selected node")
    check(node_peek._preview_scale_by_key[second_key] == 0.5,
          "reset selected preserves other node sizes")

    node_peek._preview_scale_by_key.clear()
    check(not node_peek._preview_scale_by_key,
          "reset all restores every preview size")

    node_peek._set_selected_preview_scale(context, 25.0)
    check(node_peek._preview_scale_by_key[first_key] == 0.25,
          "minimum size is accepted")
    node_peek._set_selected_preview_scale(context, 300.0)
    check(node_peek._preview_scale_by_key[first_key] == 3.0,
          "maximum size is accepted")
    node_peek._set_selected_preview_scale(context, 100.0)
    check(first_key not in node_peek._preview_scale_by_key,
          "100 percent uses the implicit default")

    print("PREVIEW_RESIZE_OK")
finally:
    node_peek.unregister()
    shutil.rmtree(test_dir, ignore_errors=True)
