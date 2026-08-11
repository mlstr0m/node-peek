"""Regression test for color-managed preview image drawing.

Run:
    blender --background --factory-startup --python test_preview_colorspace.py
"""
import importlib.util
import os
from types import SimpleNamespace

import bpy


repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "node_peek_colorspace_test", os.path.join(repo, "__init__.py"))
node_peek = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node_peek)


class FakeShader:
    def bind(self):
        pass

    def uniform_sampler(self, _name, _texture):
        pass


class FakeBatch:
    def draw(self, _shader):
        pass


material = bpy.data.materials.new("PreviewColorspaceTestMaterial")
material.use_nodes = True
node = material.node_tree.nodes.new("ShaderNodeTexNoise")
node.name = "Preview"

shader_names = []
real_bpy = node_peek.bpy
real_gpu = node_peek.gpu
real_batch_for_shader = node_peek.batch_for_shader
try:
    fake_space = SimpleNamespace(
        type='NODE_EDITOR',
        tree_type='ShaderNodeTree',
        edit_tree=material.node_tree,
        id=material,
        path=[],
    )
    fake_context = SimpleNamespace(
        space_data=fake_space,
        region=SimpleNamespace(
            view2d=SimpleNamespace(
                view_to_region=lambda x, y, clip=False: (x, y))),
        preferences=SimpleNamespace(system=SimpleNamespace(ui_scale=1.0)),
    )
    fake_shader_module = SimpleNamespace(
        from_builtin=lambda name: (shader_names.append(name) or FakeShader()))
    node_peek.bpy = SimpleNamespace(context=fake_context, types=real_bpy.types)
    node_peek.gpu = SimpleNamespace(
        shader=fake_shader_module,
        state=SimpleNamespace(blend_set=lambda _mode: None),
    )
    node_peek.batch_for_shader = lambda *_args, **_kwargs: FakeBatch()
    node_peek._prefs = lambda: SimpleNamespace(visible_by_default=True)
    node_peek._textures_material = material.name
    node_peek._node_pngpath.clear()
    node_peek._tex_by_path.clear()
    node_peek._node_pngpath[node.name] = "preview.png"
    node_peek._tex_by_path["preview.png"] = object()

    node_peek._draw_callback()
    expected = 'IMAGE_SCENE_LINEAR_TO_REC709_SRGB'
    print(("PASS " if shader_names == [expected] else "FAIL ") +
          "scene-linear preview uses the display conversion shader")
    if shader_names != [expected]:
        raise AssertionError(shader_names)
    print("PREVIEW_COLORSPACE_OK")
finally:
    node_peek.bpy = real_bpy
    node_peek.gpu = real_gpu
    node_peek.batch_for_shader = real_batch_for_shader
    node_peek._node_pngpath.clear()
    node_peek._tex_by_path.clear()
    bpy.data.materials.remove(material)
