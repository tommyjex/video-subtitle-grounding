"""Rasterize one mixed-font line with shared baseline and a single background."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from io import BytesIO

from PIL import Image, ImageDraw

from . import layout as base
from .product_names import CATALOG_VERSION, effective_mentions


def compile_product_layout(segment, video, width, height, limits):
    text = segment["translated_text"]
    if not isinstance(text, str) or not text.strip():
        raise base.SubtitleLayoutError("missing_translation")
    if len(text) > base.MAX_TEXT_LENGTH:
        raise base.SubtitleLayoutError("text_capacity_exceeded")
    if any(unicodedata.category(c) in {"Cc", "Zl", "Zp"} for c in text):
        raise base.SubtitleLayoutError("text_must_be_single_line")
    mentions = effective_mentions(segment)
    style = segment.get("style") or {}
    if not isinstance(style, Mapping):
        raise base.SubtitleLayoutError("invalid_style")
    runs = []
    cursor = 0
    ordinary_url, _ = base._resolve_font(segment, style)

    def add(start, end, kind, url):
        if end > start:
            runs.append({"start": start, "end": end, "kind": kind, "font_type": url,
                         "text": text[start:end]})

    for mention in mentions:
        add(cursor, mention["start"], "ordinary", ordinary_url)
        substring = text[mention["start"]:mention["end"]]
        matched = re.match(r"\s+".join(map(re.escape, mention["model"].split())),
                           substring, re.IGNORECASE)
        split = mention["start"] + matched.end() if matched else mention["start"]
        add(mention["start"], split, "product_model", base.FONT_RESOURCES["AvenirLTStdBlack"])
        add(split, mention["end"], "product_spec", base.FONT_RESOURCES["AvenirLTStdLight"])
        cursor = mention["end"]
    add(cursor, len(text), "ordinary", ordinary_url)
    warnings = []
    resources = {}
    # Fallback is local and glyph-checked, never an opaque vendor preset.
    expanded = []
    for run in runs:
        resource = base._load_font_resource(run["font_type"])
        resources[resource.url] = resource
        for index in range(run["start"], run["end"]):
            char = text[index]
            actual = resource
            if not char.isspace() and ord(char) not in actual.codepoints:
                fallback_id = "SY_Black_JP" if video.get("target_language") == "ja" else "SY_Black"
                actual = base._load_font_resource(base.FONT_RESOURCES[fallback_id])
                if ord(char) not in actual.codepoints:
                    raise base.SubtitleLayoutError("product_glyph_unavailable")
                resources[actual.url] = actual
                warnings.append("product_glyph_font_fallback")
            if (expanded and expanded[-1]["font_type"] == actual.url and
                    expanded[-1]["kind"] == run["kind"] and expanded[-1]["end"] == index):
                expanded[-1]["end"] += 1
                expanded[-1]["text"] += char
            else:
                expanded.append({**run, "start": index, "end": index + 1, "text": char,
                                 "font_type": actual.url, "font_sha256": actual.sha256})
    runs = expanded
    original = base._bbox(segment, width, height)
    alignment = style.get("alignment") or segment.get("alignment") or "center"
    if alignment not in {"left", "center", "right"}:
        alignment = "center"
        warnings.append("alignment_defaulted")
    box_height = original[3] - original[1]

    def measure(size):
        x, left, right, ascent, descent = 0.0, 0.0, 0.0, 0, 0
        positions = []
        for run in runs:
            font = resources[run["font_type"]].font(size)
            a, d = font.getmetrics()
            bounds = font.getbbox(run["text"], anchor="ls")
            positions.append((font, x, bounds))
            left = min(left, x + bounds[0])
            right = max(right, x + bounds[2], x + font.getlength(run["text"]))
            ascent, descent = max(ascent, a, -bounds[1]), max(descent, d, bounds[3])
            x += font.getlength(run["text"])
        return right - left + 4, ascent + descent + 4, left, ascent, positions

    def largest_fitting(maximum, fits):
        low, high = 0, maximum
        while low < high:
            middle = (low + high + 1) // 2
            if fits(middle):
                low = middle
            else:
                high = middle - 1
        return low

    upper = largest_fitting(box_height * 4, lambda size: measure(size)[1] <= box_height)
    if not upper:
        raise base.SubtitleLayoutError("bbox_below_font_minimum")
    estimated = upper
    if style.get("font_size_px") is not None:
        requested = base._number(style["font_size_px"], "invalid_font_size")
        if requested <= 0:
            raise base.SubtitleLayoutError("invalid_font_size")
        upper = min(upper, max(1, math.floor(requested)))
    desired = math.ceil(measure(upper)[0])
    box = base._expand_box(original, desired, alignment, limits)
    box_width = box[2] - box[0]
    effective = largest_fitting(upper, lambda size: measure(size)[0] <= box_width)
    if not effective:
        raise base.SubtitleLayoutError("text_cannot_fit_at_minimum_font")
    if box != original:
        warnings.extend(["bbox_expanded", "expanded_background_unverified"])
    if desired > box_width:
        warnings.append("bbox_expansion_limited")
    if effective < upper:
        warnings.append("font_size_reduced")
    if effective < 12:
        warnings.append("font_size_small")
    if limits[2]:
        warnings.append("original_bbox_overlap")
    recommended, color, panel = base._colors(segment, style, warnings)
    measured_width, measured_height, bearing, ascent, positions = measure(effective)
    x = ({"left": 0, "center": (box_width - measured_width) / 2,
          "right": box_width - measured_width}[alignment])
    y = (box_height - measured_height) / 2
    baseline = y + 2 + ascent
    image = Image.new("RGBA", (box_width, box_height))
    draw = ImageDraw.Draw(image)
    draw.rectangle((x, y, x + measured_width - 1, y + measured_height - 1), fill=panel)
    for run, (font, advance, _) in zip(runs, positions, strict=True):
        origin = x + 2 - bearing + advance
        draw.text((origin, baseline), run["text"], font=font, fill=color, anchor="ls")
        run.update({"x_px": origin, "baseline_px": baseline, "font_size_px": effective,
                    "advance_px": font.getlength(run["text"])})
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    content = buffer.getvalue()
    fingerprint = {"text": text, "runs": runs, "box": box, "video": [width, height],
                   "color": color, "panel": panel, "alignment": alignment,
                   "layout_version": base.LAYOUT_VERSION, "catalog_version": CATALOG_VERSION}
    layer_id = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode() + content).hexdigest()
    summary = {
        "segment_id": segment["segment_id"], "original_bbox_pixels": list(original),
        "render_bbox_pixels": list(box), "estimated_font_size_px": estimated,
        "target_font_size_px": upper, "effective_font_size_px": effective,
        "desired_text_width_px": desired, "measured_text_width_px": measured_width,
        "recommended_text_color": recommended, "text_color": color,
        "background_color": panel, "alignment": alignment, "font_type": None,
        "font_sha256": None, "text_runs": runs, "render_layer_asset_id": layer_id,
        "render_layer_url": None, "product_mentions": mentions,
        "ascent_px": ascent, "descent_px": measured_height - 4 - ascent,
        "warnings": sorted(set(warnings)),
    }
    element = {"type": "image", "source": f"subtitle-layer:{layer_id}", "extra": [{
        "type": "transform", "pos_x": box[0], "pos_y": box[1], "width": box_width,
        "height": box_height, "rotation": 0,
    }]}
    return element, summary, {"id": layer_id, "content": content, "content_type": "image/png"}


async def materialize_layers(compiled, materializer):
    """Resolve every internal source before submitting a project to a provider."""
    for layer_id, layer in compiled.get("layers", {}).items():
        source = await materializer(layer) if materializer else None
        if not isinstance(source, str) or not source.startswith("https://"):
            raise base.SubtitleLayoutError("product_layer_materialization_unavailable")
        for track in compiled["project"]["track"]:
            for element in track:
                if element.get("source") == f"subtitle-layer:{layer_id}":
                    element["source"] = source
