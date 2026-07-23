"""Inspect rendered PNG pixels for the normalization end-to-end test.

Run by e2e_real.py in a clean Blender process:
    blender --background --factory-startup --python-exit-code 1 \
        --python verify_normalized_preview.py -- RAW.png NORMALIZED.png
"""
from array import array
import os
import sys

import bpy


argv = sys.argv[sys.argv.index("--") + 1:]
raw_path, normalized_path = argv


def luminance_map(path):
    image = bpy.data.images.load(path, check_existing=False)
    width, height = image.size
    pixels = array("f", [0.0]) * (width * height * 4)
    image.pixels.foreach_get(pixels)
    values = []
    for i in range(0, len(pixels), 4):
        values.append(
            0.2126 * pixels[i]
            + 0.7152 * pixels[i + 1]
            + 0.0722 * pixels[i + 2])
    return width, height, values


def edge_mean(values, width, height, edge):
    if edge == "left":
        coords = ((x, y) for y in range(height)
                  for x in range(max(1, width // 16)))
    elif edge == "right":
        coords = ((x, y) for y in range(height)
                  for x in range(width - max(1, width // 16), width))
    elif edge == "bottom":
        coords = ((x, y) for y in range(max(1, height // 16))
                  for x in range(width))
    else:
        coords = ((x, y)
                  for y in range(height - max(1, height // 16), height)
                  for x in range(width))
    samples = [values[y * width + x] for x, y in coords]
    return sum(samples) / len(samples)


raw_width, raw_height, raw = luminance_map(raw_path)
norm_width, norm_height, normalized = luminance_map(normalized_path)
assert (raw_width, raw_height) == (norm_width, norm_height), \
    "raw and normalized preview sizes differ"

edge_values = [
    edge_mean(normalized, norm_width, norm_height, edge)
    for edge in ("left", "right", "bottom", "top")
]
edge_span = max(edge_values) - min(edge_values)
raw_bright = sum(value > 0.9 for value in raw) / len(raw)
normalized_bright = (
    sum(value > 0.9 for value in normalized) / len(normalized))

# The HDR fixture is a 0..4 gradient rendered on a plane that fills the frame.
# Without normalization most pixels clip white. With normalization, opposite
# frame edges must span the gradient and far fewer pixels remain clipped.
assert edge_span > 0.5, (
    f"normalized preview does not fill the frame with a gradient "
    f"(edge span {edge_span:.3f})")
assert raw_bright > 0.6, (
    f"HDR control preview is not predominantly clipped "
    f"({raw_bright:.1%} bright pixels)")
assert normalized_bright < 0.3, (
    f"normalized preview remains clipped "
    f"({normalized_bright:.1%} bright pixels)")
assert os.path.getsize(raw_path) != os.path.getsize(normalized_path), \
    "normalization did not change the rendered PNG"

print(
    "NORMALIZED_PREVIEW_OK "
    f"edge_span={edge_span:.3f} "
    f"raw_bright={raw_bright:.1%} "
    f"normalized_bright={normalized_bright:.1%}")
