"""Default PNG rendering, font fidelity, footnote expansion and resilient fallback."""

import asyncio
from copy import deepcopy
from io import BytesIO
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from video_subtitle_grounding import layout
from video_subtitle_grounding.mediakit import MultiTrackTask
from video_subtitle_grounding.render import SubtitleRenderService
from test_layout import font_http, segment, video
from test_product_layout import actual_fonts

__all__ = ["font_http", "actual_fonts"]


@pytest.mark.parametrize("role", ["headline", "body", "footnote", None])
def test_default_rgba_preserves_font_size_time_and_transform(font_http, role):
    source = segment(visual_role=role, translated_text="Visible subtitle")
    before = deepcopy(source)
    result = layout.compile_subtitle_project([source], video(video_category="product"))
    native = layout.compile_text_subtitle_project([source], video(video_category="product"))
    summary = result["layouts"]["line-1"]
    fallback = native["layouts"]["line-1"]
    element = result["project"]["track"][1][0]
    assert element["type"] == "image" and "bold" not in element
    assert element["target_time"] == [100, 1000]
    assert summary["font_type"] == fallback["font_type"]
    assert summary["font_sha256"] == fallback["font_sha256"]
    assert summary["effective_font_size_px"] == fallback["effective_font_size_px"]
    assert summary["render_bbox_pixels"] == fallback["render_bbox_pixels"]
    png = Image.open(BytesIO(next(iter(result["layers"].values()))["content"]))
    transform = element["extra"][0]
    assert png.mode == "RGBA" and png.size == (transform["width"], transform["height"])
    assert summary["raster_scale"] == 4
    assert source == before and not native["layers"]


@pytest.mark.parametrize("manual,size", [(None, 24), (18, 18), (30, 30)])
def test_footnote_grows_small_original_box_and_keeps_manual_size(font_http, manual, size):
    source = segment(visual_role="footnote", translated_text="A readable footnote",
                     bbox_normalized=[100, 900, 500, 925],
                     style={} if manual is None else {"font_size_px": manual})
    summary = layout.compile_subtitle_project([source], video())["layouts"]["line-1"]
    assert summary["effective_font_size_px"] == size
    assert summary["render_bbox_pixels"][3] - summary["render_bbox_pixels"][1] > 18
    assert summary["render_bbox_pixels"][3] <= 720


def test_footnote_respects_adjacent_line_and_canvas_edge(font_http):
    body = segment(segment_id="body", translated_text="Body", bbox_normalized=[100, 930, 600, 970])
    footnote = segment(visual_role="footnote", translated_text="Note",
                       bbox_normalized=[100, 977, 600, 999])
    result = layout.compile_subtitle_project([body, footnote], video())["layouts"]
    note = result["line-1"]
    assert note["render_bbox_pixels"][1] >= result["body"]["render_bbox_pixels"][3]
    assert note["render_bbox_pixels"][3] <= 720
    assert note["effective_font_size_px"] < 24
    assert "footnote_space_limited" in note["warnings"]


@pytest.mark.parametrize("role", ["headline", "body", "footnote"])
def test_actual_font_edges_have_partial_alpha_without_dark_halo(actual_fonts, role):
    source = segment(visual_role=role, translated_text="Übermäßig readable",
                     background_color="#000000")
    result = layout.compile_subtitle_project([source], video(video_category="product"))
    summary = result["layouts"]["line-1"]
    png = Image.open(BytesIO(next(iter(result["layers"].values()))["content"]))
    pixels = list(png.getdata())
    assert any(0 < a < 255 for r, g, b, a in pixels)
    assert all(min(r, g, b) >= 250 for r, g, b, a in pixels if a > 0)
    expected = layout.CENTURY_GOTHIC_BOLD_URL if role == "headline" else layout.CENTURY_GOTHIC_URL
    assert summary["font_sha256"] == actual_fonts[expected].sha256


def test_ordinary_storage_failure_falls_back_and_cancellation_propagates(font_http):
    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="offline", status="queued")
    renderer = SubtitleRenderService(client=client)
    asyncio.run(renderer.submit([segment()], video(), idempotency_key="offline"))
    assert client.submit.call_args.kwargs["project"]["track"][1][0]["type"] == "text"

    async def cancel(layer):
        raise asyncio.CancelledError()

    renderer.layer_materializer = cancel
    client.submit.reset_mock()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(renderer.submit([segment()], video(), idempotency_key="cancel"))
    client.submit.assert_not_called()
