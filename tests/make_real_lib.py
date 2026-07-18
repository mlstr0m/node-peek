"""Realistic custom-node add-on simulation + lib writer.
Patterns copied from real-world add-ons: per-instance trees, a property whose
update() mutates the tree, a shared-tree type, a custom node inside a regular
group, and a pure-Python (non-group) node that can't render in Cycles.
Uses the REAL node_peek._custom_node_types() collector.

Run: blender --background --factory-startup --python make_real_lib.py -- OUTDIR
"""
import os
import sys
import json
import importlib.util

import bpy

# load the add-on module from the repo root (works whatever the clone is named)
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "node_peek", os.path.join(_repo, "__init__.py"))
node_peek = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(node_peek)  # import only: no registration at import

outdir = sys.argv[sys.argv.index("--") + 1]


class SimScaledChecker(bpy.types.ShaderNodeCustomGroup):
    """Per-instance tree + property-driven rebuild (most common pattern)."""
    bl_idname = "SimScaledChecker"
    bl_label = "Sim Scaled Checker"

    def _rebuild(self, _context=None):
        self.node_tree.nodes["Checker Texture"].inputs["Scale"] \
            .default_value = self.scale

    scale: bpy.props.FloatProperty(name="Scale", default=6.0, update=_rebuild)

    def init(self, _context):
        t = bpy.data.node_groups.new(".sim_checker", "ShaderNodeTree")
        t.interface.new_socket("Color", in_out='OUTPUT',
                               socket_type="NodeSocketColor")
        c = t.nodes.new("ShaderNodeTexChecker")
        go = t.nodes.new("NodeGroupOutput")
        t.links.new(c.outputs["Color"], go.inputs[0])
        self.node_tree = t
        self._rebuild()


class SimGradient(bpy.types.ShaderNodeCustomGroup):
    bl_idname = "SimGradient"
    bl_label = "Sim Gradient"

    def init(self, _context):
        t = bpy.data.node_groups.new(".sim_grad", "ShaderNodeTree")
        t.interface.new_socket("Fac", in_out='OUTPUT',
                               socket_type="NodeSocketFloat")
        g = t.nodes.new("ShaderNodeTexGradient")
        go = t.nodes.new("NodeGroupOutput")
        t.links.new(g.outputs["Fac"], go.inputs[0])
        self.node_tree = t


class SimAnnotation(bpy.types.Node):
    """Pure-Python node, no internal tree: unrenderable in Cycles anywhere."""
    bl_idname = "SimAnnotation"
    bl_label = "Sim Annotation"

    @classmethod
    def poll(cls, ntree):
        return ntree.bl_idname == "ShaderNodeTree"

    def init(self, _context):
        self.outputs.new("NodeSocketColor", "Color")


for c in (SimScaledChecker, SimGradient, SimAnnotation):
    bpy.utils.register_class(c)

# --- material exercising every case ---
mat = bpy.data.materials.new("RealSim")
mat.use_nodes = True
nt = mat.node_tree
bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')

a1 = nt.nodes.new("SimScaledChecker"); a1.name = "CheckerA"; a1.scale = 4.0
a2 = nt.nodes.new("SimScaledChecker"); a2.name = "CheckerB"; a2.scale = 24.0
grad = nt.nodes.new("SimGradient"); grad.name = "Grad"
inv = nt.nodes.new("ShaderNodeInvert"); inv.name = "InvertA"
ann = nt.nodes.new("SimAnnotation"); ann.name = "Note"
gamma = nt.nodes.new("ShaderNodeGamma"); gamma.name = "AfterNote"

# custom -> builtin -> BSDF (downstream through a custom node)
nt.links.new(a1.outputs[0], inv.inputs["Color"])
nt.links.new(inv.outputs["Color"], bsdf.inputs["Base Color"])
nt.links.new(grad.outputs[0], bsdf.inputs["Roughness"])
# unrenderable python node -> builtin (downstream flat, like the user's render)
nt.links.new(ann.outputs[0], gamma.inputs["Color"])

# custom node INSIDE a regular group
g = bpy.data.node_groups.new("WrapGroup", "ShaderNodeTree")
g.interface.new_socket("Color", in_out='OUTPUT', socket_type="NodeSocketColor")
inner = g.nodes.new("SimGradient"); inner.name = "InnerGrad"
ggo = g.nodes.new("NodeGroupOutput")
g.links.new(inner.outputs[0], ggo.inputs[0])
wrap = nt.nodes.new("ShaderNodeGroup"); wrap.name = "Wrap"; wrap.node_tree = g

# plain second material (regression: the no-custom path must be untouched)
mat2 = bpy.data.materials.new("PlainMat")
mat2.use_nodes = True
nt2 = mat2.node_tree
bsdf2 = next(n for n in nt2.nodes if n.type == 'BSDF_PRINCIPLED')
chk = nt2.nodes.new("ShaderNodeTexChecker"); chk.name = "PlainChecker"
nt2.links.new(chk.outputs["Color"], bsdf2.inputs["Base Color"])

# the REAL production collector
customs = node_peek._custom_node_types(mat)
customs_plain = node_peek._custom_node_types(mat2)
print("CUSTOM_TYPES RealSim :", customs)
print("CUSTOM_TYPES PlainMat:", customs_plain)
assert customs == ["SimGradient", "SimScaledChecker"], customs
assert customs_plain == [], customs_plain

bpy.data.libraries.write(outdir + "/real.blend", {mat, mat2},
                         path_remap='ABSOLUTE', fake_user=True)
with open(outdir + "/customs.json", "w") as fh:
    json.dump(customs, fh)
print("PHASE_A_OK")
