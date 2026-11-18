import asyncio
import hashlib
import os
from copy import deepcopy
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from video_subtitle_grounding import layout
from video_subtitle_grounding.product_layout import materialize_layers
from video_subtitle_grounding.product_names import text_fingerprint
from test_layout import font_http, segment, video

__all__ = ["font_http"]


def product_segment():
    text = "Try FLOOR ONE i5 Lite"
    return segment(translated_text=text, visual_role="headline", product_mentions=[{
        "start": 4, "end": len(text), "model": "FLOOR ONE i5", "spec": "Lite",
        "origin": "model_pattern", "catalog_id": None,
    }], product_text_sha256=text_fingerprint(text))


def test_mixed_fonts_shared_baseline_single_rgba_board_and_cache(font_http):
    original = product_segment()
    before = deepcopy(original)
    compiled = layout.compile_subtitle_project([original], video())
    summary = compiled["layouts"]["line-1"]
    runs = summary["text_runs"]
    assert {r["kind"] for r in runs} == {"ordinary", "product_model", "product_spec"}
    assert len({r["baseline_px"] for r in runs}) == 1
    assert len({r["font_size_px"] for r in runs}) == 1
    assert "".join(r["text"] for r in runs) == original["translated_text"]
    assert [r["font_type"] for r in runs] == [
        layout.CENTURY_GOTHIC_BOLD_URL, layout.FONT_RESOURCES["AvenirLTStdBlack"],
        layout.FONT_RESOURCES["AvenirLTStdLight"],
    ]
    layer = next(iter(compiled["layers"].values()))
    png = Image.open(BytesIO(layer["content"]))
    assert png.mode == "RGBA"
    assert png.getpixel((0, 0))[3] == 0
    assert png.getbbox() is not None
    assert original == before
    again = layout.compile_subtitle_project([original], video())
    assert list(again["layers"]) == list(compiled["layers"])
    changed = layout.compile_subtitle_project(
        [{**original, "style": {"text_color": "#FF0000"}}], video())
    assert list(changed["layers"]) != list(compiled["layers"])
    disabled = layout.compile_subtitle_project(
        [{**original, "product_names_enabled": False}], video())
    assert disabled["layers"]
    assert disabled["project"]["track"][1][0]["type"] == "image"
    assert {r["kind"] for r in disabled["layouts"]["line-1"]["text_runs"]} == {"ordinary"}


def test_materializer_required_and_source_never_persisted_in_summary(font_http):
    compiled = layout.compile_subtitle_project([product_segment()], video())
    with pytest.raises(layout.SubtitleLayoutError, match="materialization"):
        asyncio.run(materialize_layers(compiled, None))

    async def upload(layer):
        assert layer["content"].startswith(b"\x89PNG")
        return "https://private.example/layer.png?signature=ephemeral"

    asyncio.run(materialize_layers(compiled, upload))
    assert compiled["project"]["track"][1][0]["source"].startswith("https://")
    assert "ephemeral" not in str(compiled["layouts"])


@pytest.mark.parametrize("text", ["FLOOR ONE S7\nStretch Steam",
                                  "FLOOR ONE S7\u2028Stretch Steam",
                                  "FLOOR ONE S7 " + "a" * 4000])
@pytest.mark.parametrize("category", ["product", "unboxing"])
def test_product_text_obeys_common_preflight(font_http, text, category):
    with pytest.raises(layout.SubtitleLayoutError):
        layout.compile_subtitle_project(
            [segment(translated_text=text)], video(video_category=category))


def test_context_spec_only_renders_light_and_invalidates_on_exclusion(font_http):
    model = segment(translated_text="FLOOR ONE S7", block_id="a")
    spec = segment(segment_id="spec", translated_text="Stretch Steam", block_id="a",
                   product_text_sha256=text_fingerprint("Stretch Steam"), product_mentions=[{
                       "start": 0, "end": 13, "model": "", "spec": "Stretch Steam",
                       "context_model": "FLOOR ONE S7", "origin": "model_pattern"}])
    compiled = layout.compile_subtitle_project([model, spec], video())
    assert {r["kind"] for r in compiled["layouts"]["spec"]["text_runs"]} == {"product_spec"}
    changed = layout.compile_subtitle_project([{**model, "excluded": True}, spec], video())
    assert changed["layers"]
    assert {r["kind"] for r in changed["layouts"]["spec"]["text_runs"]} == {"ordinary"}


@pytest.mark.parametrize("role", ["headline", "body", "footnote", None])
def test_render_resolves_layer_or_falls_back_before_provider_submit(font_http, role):
    from unittest.mock import AsyncMock
    from video_subtitle_grounding.mediakit import MultiTrackTask
    from video_subtitle_grounding.render import SubtitleRenderService

    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="fixture", status="queued")
    renderer = SubtitleRenderService(client=client)
    source = {**product_segment(), "visual_role": role}
    result = asyncio.run(renderer.submit([source], video(), idempotency_key="fallback"))
    assert "product_layer_materialization_unavailable" in result["warnings"]
    fallback = client.submit.call_args.kwargs["project"]["track"][1][0]
    assert fallback["type"] == "text"
    assert fallback["bold"] is (role != "headline")

    async def upload(layer):
        return "https://private.example/layer.png?signature=runtime"

    renderer.layer_materializer = upload
    result = asyncio.run(renderer.submit([source], video(), idempotency_key="mixed"))
    element = client.submit.call_args.kwargs["project"]["track"][1][0]
    assert element["type"] == "image"
    assert "bold" not in element
    assert element["source"].endswith("signature=runtime")
    assert "signature=runtime" not in str(result)


@pytest.mark.parametrize("background", ["#FFFFFF", "#000000", "#777777", None])
@pytest.mark.parametrize("panel", [None, "#FF0000FF", "#33333359", "#00000000"])
def test_product_png_policy_and_legacy_cache_compatibility(font_http, background, panel):
    source = {**product_segment(), "background_color": background,
              "style": {"background_color": panel}}
    before = deepcopy(source)
    product = layout.compile_subtitle_project([source], video(video_category="product"))
    unboxing = layout.compile_subtitle_project([source], video(video_category="unboxing"))
    legacy = layout.compile_subtitle_project([source], video())
    assert unboxing == legacy
    assert source == before
    summary = product["layouts"]["line-1"]
    assert summary["background_color"] == "#00000000"
    assert summary["text_color"] == ("#000000FF" if background == "#FFFFFF" else "#FFFFFFFF")
    assert set(product["layers"]).isdisjoint(unboxing["layers"])
    without_panel = layout.compile_subtitle_project(
        [{**source, "style": {}}], video(video_category="product"))
    assert product == without_panel
    assert layout.compile_subtitle_project(
        [source], video(video_category="product")) == product


def test_png_cache_includes_layout_version_and_effective_panel(font_http, monkeypatch):
    source = {**product_segment(), "background_color": "#777777"}
    initial = layout.compile_subtitle_project([source], video())
    transparent = layout.compile_subtitle_project(
        [{**source, "style": {"background_color": "#00000000"}}], video())
    assert set(initial["layers"]).isdisjoint(transparent["layers"])
    monkeypatch.setattr(layout, "LAYOUT_VERSION", "old-layout")
    changed = layout.compile_subtitle_project([source], video())
    assert set(initial["layers"]).isdisjoint(changed["layers"])


@pytest.fixture
def actual_fonts(monkeypatch, font_http):
    directory = os.environ.get("SUBTITLE_REAL_FONT_DIR")
    if not directory:
        pytest.skip("Set SUBTITLE_REAL_FONT_DIR to cached production font files")
    root = Path(directory)
    filenames = {
        layout.CENTURY_GOTHIC_URL: "centurygothic.ttf",
        layout.CENTURY_GOTHIC_BOLD_URL: "centurygothic_bold.ttf",
        layout.FONT_RESOURCES["AvenirLTStdBlack"]: "AvenirLTStd-Black.otf",
        layout.FONT_RESOURCES["AvenirLTStdLight"]: "AvenirLTStd-Light.otf",
    }
    resources = {}
    for url, filename in filenames.items():
        content = (root / filename).read_bytes()
        resources[url] = layout._FontResource(
            url, content, hashlib.sha256(content).hexdigest(),
            layout._font_codepoints(content))
    assert len({resource.sha256 for resource in resources.values()}) == 4
    monkeypatch.setattr(layout, "_load_font_resource", resources.__getitem__)
    return resources


@pytest.mark.parametrize("role", ["headline", "body", "footnote"])
@pytest.mark.parametrize("background", ["#FFFFFF", "#000000", "#777777", None])
@pytest.mark.parametrize("manual_text", [None, "#00FF00"])
def test_actual_mixed_font_png_contains_only_text_pixels(actual_fonts, role, background, manual_text):
    text = "Try FLOOR ONE i5 Lite and FLOOR ONE S7 Stretch Steam"
    second = text.index("FLOOR ONE S7")
    source = segment(
        translated_text=text, visual_role=role, background_color=background,
        product_text_sha256=text_fingerprint(text),
        product_mentions=[
            {"start": 4, "end": text.index(" and"), "model": "FLOOR ONE i5",
             "spec": "Lite", "origin": "model_pattern"},
            {"start": second, "end": len(text), "model": "FLOOR ONE S7",
             "spec": "Stretch Steam", "origin": "model_pattern"},
        ],
        style={"background_color": "#FF0000FF", "text_color": manual_text},
        bbox_normalized=[100, 200, 900, 400],
    )
    before = deepcopy(source)
    compiled = layout.compile_subtitle_project([source], video(video_category="product"))
    assert source == before
    summary = compiled["layouts"]["line-1"]
    runs = summary["text_runs"]
    assert len(summary["product_mentions"]) == 2
    assert {run["kind"] for run in runs} == {"ordinary", "product_model", "product_spec"}
    assert len({run["baseline_px"] for run in runs}) == 1
    assert len({run["font_size_px"] for run in runs}) == 1
    assert "".join(run["text"] for run in runs) == text
    ordinary = layout.CENTURY_GOTHIC_BOLD_URL if role == "headline" else layout.CENTURY_GOTHIC_URL
    assert {run["font_type"] for run in runs} == {
        ordinary, layout.FONT_RESOURCES["AvenirLTStdBlack"], layout.FONT_RESOURCES["AvenirLTStdLight"],
    }
    png = Image.open(BytesIO(next(iter(compiled["layers"].values()))["content"])).convert("RGBA")
    assert png.getchannel("A").getextrema() == (0, 255)
    expected = Image.new("RGBA", (png.width * 4, png.height * 4))
    draw = ImageDraw.Draw(expected)
    for run in runs:
        resource = actual_fonts[run["font_type"]]
        assert run["font_sha256"] == resource.sha256
        draw.text((run["x_px"] * 4, run["baseline_px"] * 4), run["text"],
                  font=resource.font(run["font_size_px"] * 4), fill=summary["text_color"], anchor="ls")
    expected = expected.convert("RGBa").resize(png.size, Image.Resampling.LANCZOS).convert("RGBA")
    assert ImageChops.difference(png, expected).getbbox(alpha_only=False) is None
    assert png.getpixel((0, 0)) == (0, 0, 0, 0)
    assert png.getcolors(png.width * png.height) is not None
    assert (255, 0, 0, 255) not in {
        pixel for _, pixel in png.getcolors(png.width * png.height)}
    assert summary["background_color"] == "#00000000"
    assert summary["text_color"] == (
        manual_text + "FF" if manual_text else
        "#000000FF" if background == "#FFFFFF" else "#FFFFFFFF")
    assert not any("fallback" in warning for warning in summary["warnings"])


def test_actual_unboxing_png_keeps_gray_and_custom_panel_pixels(actual_fonts):
    for panel, pixel in [(None, (51, 51, 51, 89)), ("#FF000080", (255, 0, 0, 128))]:
        source = {**product_segment(), "background_color": None,
                  "style": {"background_color": panel}}
        result = layout.compile_subtitle_project([source], video(video_category="unboxing"))
        png = Image.open(BytesIO(next(iter(result["layers"].values()))["content"]))
        assert dict((color, count) for count, color in
                    png.getcolors(png.width * png.height))[pixel] > 100
