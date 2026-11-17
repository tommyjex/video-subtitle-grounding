import asyncio
from copy import deepcopy
from io import BytesIO

import pytest
from PIL import Image

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
    assert disabled["layers"] == {}
    assert disabled["project"]["track"][1][0]["type"] == "text"


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
def test_product_text_obeys_common_preflight(font_http, text):
    with pytest.raises(layout.SubtitleLayoutError):
        layout.compile_subtitle_project([segment(translated_text=text)], video())


def test_context_spec_only_renders_light_and_invalidates_on_exclusion(font_http):
    model = segment(translated_text="FLOOR ONE S7", block_id="a")
    spec = segment(segment_id="spec", translated_text="Stretch Steam", block_id="a",
                   product_text_sha256=text_fingerprint("Stretch Steam"), product_mentions=[{
                       "start": 0, "end": 13, "model": "", "spec": "Stretch Steam",
                       "context_model": "FLOOR ONE S7", "origin": "model_pattern"}])
    compiled = layout.compile_subtitle_project([model, spec], video())
    assert {r["kind"] for r in compiled["layouts"]["spec"]["text_runs"]} == {"product_spec"}
    changed = layout.compile_subtitle_project([{**model, "excluded": True}, spec], video())
    assert changed["layers"] == {}


def test_render_resolves_layer_or_falls_back_before_provider_submit(font_http):
    from unittest.mock import AsyncMock
    from video_subtitle_grounding.mediakit import MultiTrackTask
    from video_subtitle_grounding.render import SubtitleRenderService

    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="fixture", status="queued")
    renderer = SubtitleRenderService(client=client)
    result = asyncio.run(renderer.submit([product_segment()], video(), idempotency_key="fallback"))
    assert "product_layer_materialization_unavailable" in result["warnings"]
    assert client.submit.call_args.kwargs["project"]["track"][1][0]["type"] == "text"

    async def upload(layer):
        return "https://private.example/layer.png?signature=runtime"

    renderer.layer_materializer = upload
    result = asyncio.run(renderer.submit([product_segment()], video(), idempotency_key="mixed"))
    element = client.submit.call_args.kwargs["project"]["track"][1][0]
    assert element["type"] == "image"
    assert element["source"].endswith("signature=runtime")
    assert "signature=runtime" not in str(result)
