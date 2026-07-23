"""Inspect rendered PNG pixels for the normalization end-to-end test.

Run by e2e_real.py in a clean Blender process:
    blender --background --factory-startup --python-exit-code 1 \
        --python verify_normalized_preview.py -- \
        RAW.png NORMALIZED.png IN_RANGE_RAW.png IN_RANGE_NORMALIZED.png
"""
from array import array
import sys

import bpy


argv = sys.argv[sys.argv.index("--") + 1:]
raw_path, normalized_path, in_range_raw_path, in_range_normalized_path = argv


def image_data(path):
    image = bpy.data.images.load(path, check_existing=False)
    width, height = image.size
    pixels = array("f", [0.0]) * (width * height * 4)
    image.pixels.foreach_get(pixels)
    values = []
    rgb = []
    for i in range(0, len(pixels), 4):
        rgb.extend(pixels[i:i + 3])
        values.append(
            0.2126 * pixels[i]
            + 0.7152 * pixels[i + 1]
            + 0.0722 * pixels[i + 2])
    return width, height, values, rgb


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


raw_width, raw_height, raw, _raw_rgb = image_data(raw_path)
norm_width, norm_height, normalized, _normalized_rgb = image_data(
    normalized_path)
assert (raw_width, raw_height) == (norm_width, norm_height), \
    "raw and normalized preview sizes differ"

in_width, in_height, _in_luma, in_range_rgb = image_data(in_range_raw_path)
out_width, out_height, _out_luma, in_range_normalized_rgb = image_data(
    in_range_normalized_path)
assert (in_width, in_height) == (out_width, out_height), \
    "in-range preview sizes differ"

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
in_range_max_diff = max(
    abs(before - after)
    for before, after in zip(in_range_rgb, in_range_normalized_rgb))
assert in_range_max_diff <= (1.0 / 255.0 + 1.0e-5), (
    f"in-range map was changed by normalization "
    f"(max difference {in_range_max_diff:.6f})")

print(
    "NORMALIZED_PREVIEW_OK "
    f"edge_span={edge_span:.3f} "
    f"raw_bright={raw_bright:.1%} "
    f"normalized_bright={normalized_bright:.1%} "
    f"in_range_max_diff={in_range_max_diff:.6f}")
