# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual test for custom-node support (Node Peek >= 0.5.0).

Paste this into Blender's Text Editor and press Run, with an object that has a
node material selected and a Shader Editor open.

It registers a realistic add-on-style custom node (ShaderNodeCustomGroup with
a per-instance tree and a property that rebuilds it), drops one into the
active material, and wires it to the Principled BSDF.

What you should see with Node Peek enabled:
  * a checker thumbnail above the "NP Test Checker" node;
  * the BSDF sphere preview shows the checker too (downstream works);
  * drag the node's Scale slider -> both previews update within ~1 s.

Clean up with undo, or delete the node.
"""
import bpy


class NPTestChecker(bpy.types.ShaderNodeCustomGroup):
    bl_idname = "NPTestChecker"
    bl_label = "NP Test Checker"

    def _rebuild(self, _context=None):
        self.node_tree.nodes["Checker Texture"].inputs["Scale"] \
            .default_value = self.scale

    scale: bpy.props.FloatProperty(name="Scale", default=6.0, min=0.1,
                                   update=_rebuild)

    def init(self, _context):
        t = bpy.data.node_groups.new(".np_test_checker", "ShaderNodeTree")
        t.interface.new_socket("Color", in_out='OUTPUT',
                               socket_type="NodeSocketColor")
        c = t.nodes.new("ShaderNodeTexChecker")
        go = t.nodes.new("NodeGroupOutput")
        t.links.new(c.outputs["Color"], go.inputs[0])
        self.node_tree = t
        self._rebuild()

    def draw_buttons(self, _context, layout):
        layout.prop(self, "scale")


if not hasattr(bpy.types, "NPTestChecker"):
    bpy.utils.register_class(NPTestChecker)

obj = bpy.context.object
if obj is None or obj.active_material is None \
        or obj.active_material.node_tree is None:
    raise RuntimeError("Select an object with a node-based material first")

nt = obj.active_material.node_tree
node = nt.nodes.new("NPTestChecker")
node.location = (-650, 350)
bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
if bsdf is not None:
    nt.links.new(node.outputs[0], bsdf.inputs["Base Color"])

print("NP Test Checker added — watch its Node Peek thumbnail, "
      "then drag its Scale slider to see the live update.")
