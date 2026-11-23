"""Actual-font contracts for readable, complete single-line bottom footnotes."""

import math
import asyncio
from copy import deepcopy
from io import BytesIO

import pytest
from PIL import Image

from video_subtitle_grounding import layout
from video_subtitle_grounding.product_names import text_fingerprint
from test_layout import font_http, segment, video
from test_product_layout import actual_fonts

__all__ = ["font_http", "actual_fonts"]


def bottom_note(**overrides):
    return segment(**{
        "visual_role": "footnote",
        "bbox_normalized": [480, 969, 999, 999],
        "alignment": "left",
        "background_color": "#000000",
        **overrides,
    })


def metadata(**overrides):
    return video(**{"width": 1920, "height": 1080, "video_category": "product", **overrides})


def boundary_text(resources):
    font = resources[layout.CENTURY_GOTHIC_URL].font(14)
    return "W" * math.floor((1823 - 8) / font.getlength("W"))


@pytest.mark.parametrize("mode", ["png", "text"])
@pytest.mark.parametrize("alignment", ["left", "center", "right"])
def test_bottom_footnote_moves_from_original_anchor_and_fits_at_14(
    actual_fonts, mode, alignment
):
    source = bottom_note(translated_text=boundary_text(actual_fonts))
    source["alignment"] = alignment
    before = deepcopy(source)
    result = layout.compile_subtitle_project([source], metadata(render_mode=mode))
    summary = result["layouts"]["line-1"]
    assert summary["render_bbox_pixels"][0::2] == [77, 1900]
    assert summary["alignment"] == "right"
    assert summary["effective_font_size_px"] == 14
    assert summary["measured_text_width_px"] <= 1823
    assert summary["render_bbox_pixels"][3] <= 1080
    assert summary["target_time"] == [100, 1000]
    assert "font_size_reduced" in summary["warnings"]
    assert "font_size_small" not in summary["warnings"]
    assert source == before
    element = result["project"]["track"][1][0]
    if mode == "text":
        assert element["text"] == source["translated_text"]
        assert element["font_size"] == 14 and element["line_max_width"] == -1
    else:
        assert "".join(run["text"] for run in summary["text_runs"]) == source["translated_text"]
        image = Image.open(BytesIO(next(iter(result["layers"].values()))["content"]))
        assert image.width == 1823 and image.getbbox() is not None


@pytest.mark.parametrize("manual,expected", [(None, 24), (12, 14), (14, 14), (18, 18)])
@pytest.mark.parametrize("mode", ["png", "text"])
def test_footnote_size_floor_and_larger_manual_target(actual_fonts, manual, expected, mode):
    source = bottom_note(translated_text="Readable note")
    source["style"] = {} if manual is None else {"font_size_px": manual}
    summary = layout.compile_subtitle_project(
        [source], metadata(render_mode=mode)
    )["layouts"]["line-1"]
    assert summary["effective_font_size_px"] == expected
    assert ("font_size_minimum_applied" in summary["warnings"]) is (manual == 12)


def test_mixed_product_font_uses_same_readable_region_and_preserves_all_runs(actual_fonts):
    model, spec = "FLOOR ONE i5", " Lite"
    prefix = "Try "
    width_at_14 = (
        actual_fonts[layout.CENTURY_GOTHIC_URL].font(14).getlength(prefix)
        + actual_fonts[layout.FONT_RESOURCES["AvenirLTStdBlack"]].font(14).getlength(model)
        + actual_fonts[layout.FONT_RESOURCES["AvenirLTStdLight"]].font(14).getlength(spec)
        + actual_fonts[layout.CENTURY_GOTHIC_URL].font(14).getlength(" ")
    )
    ordinary = actual_fonts[layout.CENTURY_GOTHIC_URL].font(14)
    text = prefix + model + spec + " " + "W" * math.floor(
        (1823 - width_at_14 - 12) / ordinary.getlength("W")
    )
    source = bottom_note(
        translated_text=text,
        product_text_sha256=text_fingerprint(text),
        product_mentions=[{
            "start": len(prefix), "end": len(prefix + model + spec),
            "model": model, "spec": "Lite", "origin": "model_pattern",
        }],
    )
    summary = layout.compile_subtitle_project([source], metadata())["layouts"]["line-1"]
    assert summary["render_bbox_pixels"][0::2] == [77, 1900]
    assert summary["alignment"] == "right"
    assert summary["effective_font_size_px"] == 14
    assert "".join(run["text"] for run in summary["text_runs"]) == text
    assert {run["font_type"] for run in summary["text_runs"]} == {
        layout.CENTURY_GOTHIC_URL,
        layout.FONT_RESOURCES["AvenirLTStdBlack"],
        layout.FONT_RESOURCES["AvenirLTStdLight"],
    }
    assert {run["font_size_px"] for run in summary["text_runs"]} == {14}
    assert "product_layout_unavailable" not in summary["warnings"]


def test_full_width_vertical_growth_reserves_previously_distant_neighbor(actual_fonts):
    body = segment(segment_id="body", bbox_normalized=[40, 950, 300, 972],
                   translated_text="A neighboring caption", style={"font_size_px": 18})
    note = bottom_note(translated_text="A readable footnote")
    note["bbox_normalized"] = [800, 982, 999, 999]
    result = layout.compile_subtitle_project([body, note], metadata())["layouts"]
    assert result["line-1"]["render_bbox_pixels"][1] >= result["body"]["render_bbox_pixels"][3]
    assert result["line-1"]["effective_font_size_px"] >= 14


def test_full_width_does_not_cover_same_row_neighbor(actual_fonts):
    body = segment(segment_id="body", bbox_normalized=[50, 969, 280, 999],
                   translated_text="A side caption", style={"font_size_px": 18})
    note = bottom_note(translated_text="A readable footnote")
    result = layout.compile_subtitle_project([body, note], metadata())["layouts"]
    assert result["line-1"]["render_bbox_pixels"][0] > result["body"]["render_bbox_pixels"][2]
    assert result["line-1"]["render_bbox_pixels"][2] == 1900


@pytest.mark.parametrize("mode", ["png", "text"])
def test_text_too_long_for_14_fails_locatably_without_cutting_or_shrinking(actual_fonts, mode):
    note = bottom_note(translated_text="W" * 4000)
    before = deepcopy(note)
    with pytest.raises(layout.SubtitleLayoutError) as error:
        layout.compile_subtitle_project([note], metadata(render_mode=mode))
    assert error.value.code == "text_cannot_fit_at_minimum_font"
    assert error.value.segment_id == note["segment_id"]
    assert note == before


@pytest.mark.parametrize("role", ["headline", "body"])
def test_non_footnote_manual_size_and_anchor_are_unchanged(actual_fonts, role):
    source = segment(visual_role=role, translated_text="Small",
                     style={"font_size_px": 5}, bbox_normalized=[480, 969, 999, 999])
    summary = layout.compile_subtitle_project([source], metadata())["layouts"]["line-1"]
    assert summary["effective_font_size_px"] == 5
    assert summary["render_bbox_pixels"] == summary["original_bbox_pixels"]


def test_non_bottom_footnote_preserves_spatial_anchor(actual_fonts):
    source = bottom_note(translated_text="Note")
    source["bbox_normalized"] = [480, 200, 999, 300]
    summary = layout.compile_subtitle_project([source], metadata())["layouts"]["line-1"]
    assert summary["effective_font_size_px"] == 24
    assert summary["render_bbox_pixels"] == summary["original_bbox_pixels"]
    assert summary["alignment"] == "left"


@pytest.mark.parametrize("mode", ["png", "text"])
@pytest.mark.parametrize("width", [1080, 1280, 1920, 2048])
@pytest.mark.parametrize("recognized", ["left", "center", "right", None])
@pytest.mark.parametrize("mixed", [False, True])
def test_short_bottom_note_aligns_to_canvas_right_edge(
    actual_fonts, mode, width, recognized, mixed
):
    text = "Try FLOOR ONE i5 Lite" if mixed else "*A short footnote"
    source = bottom_note(translated_text=text, alignment=recognized)
    if mixed:
        source.update(
            product_text_sha256=text_fingerprint(text),
            product_mentions=[{
                "start": 4, "end": len(text), "model": "FLOOR ONE i5",
                "spec": "Lite", "origin": "model_pattern",
            }],
        )
    before = deepcopy(source)
    compiled = layout.compile_subtitle_project(
        [source], metadata(width=width, render_mode=mode)
    )
    summary = compiled["layouts"]["line-1"]
    right = width - math.ceil(width * 0.01)
    left, _, box_right, _ = summary["render_bbox_pixels"]
    assert left == math.ceil(width * 0.04) and box_right == right
    assert summary["alignment"] == "right"
    assert summary["effective_font_size_px"] == 24
    assert source == before
    element = compiled["project"]["track"][1][0]
    if mode == "text":
        assert element["align_type"] == 2
        assert element["text"] == text
    else:
        image = Image.open(BytesIO(compiled["layers"][summary["render_layer_asset_id"]]["content"]))
        ink = image.getchannel("A").getbbox()
        assert ink is not None
        assert right - 8 <= left + ink[2] <= right
        assert left + ink[0] > width / 2
        assert "".join(run["text"] for run in summary["text_runs"]) == text
        assert ("product_layout_unavailable" in summary["warnings"]) is False
        if mixed:
            assert {run["kind"] for run in summary["text_runs"]} == {
                "ordinary", "product_model", "product_spec",
            }


@pytest.mark.parametrize("mode", ["png", "text"])
@pytest.mark.parametrize("manual", ["left", "center", "right"])
@pytest.mark.parametrize("mixed", [False, True])
def test_bottom_note_manual_alignment_still_wins(actual_fonts, mode, manual, mixed):
    text = "FLOOR ONE i5 Lite" if mixed else "A short footnote"
    source = bottom_note(
        translated_text=text, alignment="center", style={"alignment": manual}
    )
    if mixed:
        source.update(
            product_text_sha256=text_fingerprint(text),
            product_mentions=[{
                "start": 0, "end": len(text), "model": "FLOOR ONE i5",
                "spec": "Lite", "origin": "model_pattern",
            }],
        )
    compiled = layout.compile_subtitle_project([source], metadata(render_mode=mode))
    summary = compiled["layouts"]["line-1"]
    assert summary["alignment"] == manual
    if mode == "text":
        assert compiled["project"]["track"][1][0]["align_type"] == {
            "left": 0, "center": 1, "right": 2,
        }[manual]
    else:
        image = Image.open(BytesIO(compiled["layers"][summary["render_layer_asset_id"]]["content"]))
        ink = image.getchannel("A").getbbox()
        assert ink is not None
        if manual == "left":
            assert ink[0] <= 8
        elif manual == "center":
            assert abs((ink[0] + ink[2]) / 2 - image.width / 2) <= 8
        else:
            assert image.width - ink[2] <= 8


@pytest.mark.parametrize("manual", ["legacy", ["right"], None, ""])
def test_invalid_or_empty_manual_alignment_uses_right_default(actual_fonts, manual):
    source = bottom_note(style={"alignment": manual}, translated_text="Note")
    summary = layout.compile_subtitle_project([source], metadata())["layouts"]["line-1"]
    assert summary["alignment"] == "right"


def test_right_note_reserves_simultaneous_right_neighbor(actual_fonts):
    source = bottom_note(
        bbox_normalized=[200, 969, 700, 999], translated_text="Note"
    )
    neighbor = segment(
        segment_id="neighbor", bbox_normalized=[800, 969, 990, 999],
        translated_text="Nearby", alignment="left", style={"font_size_px": 18},
    )
    result = layout.compile_subtitle_project([source, neighbor], metadata())["layouts"]
    note, other = result["line-1"], result["neighbor"]
    assert note["alignment"] == "right"
    assert note["render_bbox_pixels"][2] < other["render_bbox_pixels"][0]
    assert note["render_bbox_pixels"][2] < 1900


@pytest.mark.parametrize("mixed", [False, True])
def test_png_materialization_failure_keeps_native_right_alignment(actual_fonts, mixed):
    from unittest.mock import AsyncMock
    from video_subtitle_grounding.mediakit import MultiTrackTask
    from video_subtitle_grounding.render import SubtitleRenderService

    text = "Try FLOOR ONE i5 Lite" if mixed else "A readable footnote"
    source = bottom_note(translated_text=text, alignment="center")
    if mixed:
        source.update(
            product_text_sha256=text_fingerprint(text),
            product_mentions=[{
                "start": 4, "end": len(text), "model": "FLOOR ONE i5",
                "spec": "Lite", "origin": "model_pattern",
            }],
        )
    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="fixture", status="queued")
    result = asyncio.run(SubtitleRenderService(client=client).submit(
        [source], metadata(), idempotency_key="footnote-fallback",
    ))
    element = client.submit.call_args.kwargs["project"]["track"][1][0]
    assert result["effective_render_mode"] == "text"
    assert element["type"] == "text" and element["align_type"] == 2
    assert element["text"] == text
    assert element["extra"][0]["pos_x"] + element["extra"][0]["width"] == 1900
