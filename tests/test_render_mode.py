"""Offline requested/effective mode, encoding and API compatibility contracts."""

import asyncio
import json
import logging
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from video_subtitle_grounding import layout
from video_subtitle_grounding.mediakit import MultiTrackTask
from video_subtitle_grounding.logging import JsonFormatter, log_event, tls_only_logger
from video_subtitle_grounding.render import SubtitleRenderService
from video_subtitle_grounding.translation_schemas import SubtitleRenderRequest
from test_layout import font_http, segment, video
from test_product_layout import actual_fonts, product_segment
from test_render_api import KEY, app_for

__all__ = ["font_http", "actual_fonts"]


@pytest.mark.parametrize("category", ["product", "unboxing"])
@pytest.mark.parametrize("role", ["headline", "body", "footnote", None, "unknown"])
@pytest.mark.parametrize("manual", [False, True])
def test_text_runtime_keeps_native_policy_without_raster(font_http, monkeypatch, category, role, manual):
    def forbidden(*args, **kwargs):
        pytest.fail("Text must not rasterize")

    monkeypatch.setattr("video_subtitle_grounding.raster.rasterize_text", forbidden)
    monkeypatch.setattr("video_subtitle_grounding.product_layout.compile_product_layout", forbidden)
    source = segment(visual_role=role, translated_text="Visible subtitle",
                     background_color="#777777",
                     style={"font": "AvenirLTStdLight", "font_size_px": 18} if manual else {})
    before = deepcopy(source)
    result = layout.compile_subtitle_project(
        [source], video(render_mode="text", video_category=category, video_bit_rate=7000000))
    summary = result["layouts"]["line-1"]
    element = result["project"]["track"][1][0]
    assert source == before
    assert not result["layers"]
    assert result["render_mode"] == summary["render_mode"] == "text"
    assert result["effective_render_mode"] == summary["effective_render_mode"] == "text"
    assert element["type"] == "text" and element["text"] == source["translated_text"]
    assert element["bold"] is (role != "headline")
    assert element["line_max_width"] == -1 and element["typesetting"] == 0
    assert element["target_time"] == [100, 1000]
    assert "background_color" not in element
    assert summary["background_color"] == (
        "#00000000" if category == "product" else layout.DEFAULT_PANEL_COLOR)
    assert result["project"]["output"]["codec"] == {"video_codec": "h264", "crf": 16}
    assert "source_video_bit_rate_unavailable" not in result["warnings"]
    if manual:
        assert summary["effective_font_size_px"] == 18
        assert element["font_type"] == layout.FONT_RESOURCES["AvenirLTStdLight"]
    elif role == "footnote":
        assert summary["effective_font_size_px"] == 24


@pytest.mark.parametrize("requested", ["png", "text"])
def test_explicit_text_forces_native_and_preserves_requested_mode(font_http, requested):
    result = layout.compile_text_subtitle_project([segment()], video(render_mode=requested))
    assert not result["layers"]
    assert result["render_mode"] == requested
    assert result["effective_render_mode"] == "text"


@pytest.mark.parametrize("mode", [None, "", "PNG", 1, [], {}])
def test_runtime_rejects_invalid_mode(font_http, mode):
    with pytest.raises(layout.SubtitleLayoutError, match="invalid_render_mode"):
        layout.compile_subtitle_project([segment()], video(render_mode=mode))


def test_product_text_warns_only_for_effective_mentions_and_preserves_data(font_http):
    source = product_segment()
    before = deepcopy(source)
    result = layout.compile_subtitle_project([source], video(render_mode="text"))
    assert source == before and not result["layers"]
    assert "text_product_font_fallback" in result["layouts"][source["segment_id"]]["warnings"]
    disabled = layout.compile_subtitle_project(
        [{**source, "product_names_enabled": False}], video(render_mode="text"))
    assert "text_product_font_fallback" not in disabled["layouts"][source["segment_id"]]["warnings"]


def test_png_reports_partial_and_all_text_fallback(font_http, monkeypatch):
    from video_subtitle_grounding import raster

    original = raster.rasterize_text

    def fail_one(element, summary):
        if summary["segment_id"] == "bad":
            raise OSError("offline raster failure")
        return original(element, summary)

    monkeypatch.setattr(raster, "rasterize_text", fail_one)
    bad = segment(segment_id="bad")
    result = layout.compile_subtitle_project([segment(), bad], video())
    assert result["render_mode"] == "png"
    assert result["effective_render_mode"] == "mixed"
    assert result["layouts"]["line-1"]["effective_render_mode"] == "png"
    assert result["layouts"]["bad"]["effective_render_mode"] == "text"
    assert result["layouts"]["line-1"]["raster_scale"] == 4
    assert layout.compile_subtitle_project([bad], video())["effective_render_mode"] == "text"
    missing = layout.compile_subtitle_project([segment(translated_text="Missing \U0001f600")], video())
    assert missing["effective_render_mode"] == "text" and not missing["layers"]


@pytest.mark.parametrize("mode", ["png", "text"])
def test_service_storage_fallback_or_text_bypass(font_http, monkeypatch, mode):
    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="offline", status="queued")
    materializer = AsyncMock(side_effect=OSError("offline storage failure"))
    prepare = AsyncMock(side_effect=AssertionError("Text must bypass materialization"))
    if mode == "text":
        monkeypatch.setattr("video_subtitle_grounding.product_layout.materialize_layers",
                            prepare)
    service = SubtitleRenderService(client=client, layer_materializer=materializer)
    result = asyncio.run(service.submit([product_segment()], video(render_mode=mode),
                                        idempotency_key="offline"))
    assert result["render_mode"] == mode and result["effective_render_mode"] == "text"
    assert result["encoding_policy_version"] == "configurable-crf-h264-v3"
    assert result["layouts"]["line-1"]["effective_render_mode"] == "text"
    assert "text_product_font_fallback" in result["layouts"]["line-1"]["warnings"]
    assert client.submit.call_args.kwargs["project"]["output"]["codec"] == {
        "video_codec": "h264", "crf": 16}
    if mode == "text":
        materializer.assert_not_called()
        prepare.assert_not_called()
    else:
        materializer.assert_awaited_once()


@pytest.mark.parametrize("mode", ["png", "text"])
def test_api_passes_mode_to_two_argument_custom_compiler(mode):
    calls = []

    def compiler(segments, runtime):
        calls.append(runtime)
        return {"project": {"track": [[{"type": "video", "source": runtime["source_url"]}],
                                     [{"type": "text", "text": "Test"}]],
                            "output": {"codec": {"video_bit_rate": 7000}}},
                "layouts": {"line-1": {}}, "warnings": []}

    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="offline", status="queued")
    renderer = SubtitleRenderService(client=client, compiler=compiler)
    with TestClient(app_for(renderer=renderer)) as api:
        response = api.post("/v1/subtitles/render", headers=KEY, json={
            "segments": [segment()], "video": video(), "render_mode": mode,
            "idempotency_key": "offline"})
    assert response.status_code == 200
    assert calls[0]["render_mode"] == mode
    assert response.json()["render_mode"] == mode
    assert response.json()["effective_render_mode"] == "text"
    assert client.submit.call_args.kwargs["project"]["output"]["codec"] == {
        "video_codec": "h264", "crf": 16}


def test_api_mode_defaults_png_and_rejects_explicit_null_or_invalid():
    body = {"segments": [segment()], "video": video(), "idempotency_key": "offline"}
    assert SubtitleRenderRequest(**body).render_mode == "png"
    renderer = SimpleNamespace(submit=AsyncMock(return_value={}))
    with TestClient(app_for(renderer=renderer)) as api:
        assert api.post("/v1/subtitles/render", headers=KEY, json=body).status_code == 200
        assert renderer.submit.call_args.args[1]["render_mode"] == "png"
        for invalid in (None, "invalid", 0):
            with pytest.raises(ValidationError):
                SubtitleRenderRequest(**body, render_mode=invalid)
            assert api.post("/v1/subtitles/render", headers=KEY,
                            json={**body, "render_mode": invalid}).status_code == 422


@pytest.mark.parametrize("mode", ["png", "text"])
@pytest.mark.parametrize("bitrate", [7000, 7000000, None, "broken"])
@pytest.mark.parametrize("audio,fps", [(True, 25), (False, 30)])
def test_codec_never_depends_on_metadata_and_keeps_audio_fps(font_http, mode, bitrate, audio, fps):
    compiled = layout.compile_subtitle_project(
        [segment()], video(render_mode=mode, video_bit_rate=bitrate, has_audio=audio, fps=fps))
    assert compiled["project"]["output"] == {
        "format": "mp4", "fps": fps, "disable_audio": not audio,
        "codec": {"video_codec": "h264", "crf": 16}}
    assert "source_video_bit_rate_unavailable" not in compiled["warnings"]


def test_product_generation_failure_is_native_with_warning(font_http, monkeypatch):
    def fail(*args, **kwargs):
        raise layout.SubtitleLayoutError("product_layout_unavailable")

    monkeypatch.setattr("video_subtitle_grounding.product_layout.compile_product_layout", fail)
    result = layout.compile_subtitle_project([product_segment()], video())
    assert not result["layers"]
    assert result["render_mode"] == "png" and result["effective_render_mode"] == "text"
    assert "text_product_font_fallback" in result["layouts"]["line-1"]["warnings"]
    assert result["project"]["output"]["codec"] == {"video_codec": "h264", "crf": 16}


@pytest.mark.parametrize("mode", ["png", "text"])
def test_excluded_rows_do_not_affect_effective_mode(font_http, mode):
    result = layout.compile_subtitle_project([
        segment(), segment(segment_id="excluded", excluded=True),
        segment(segment_id="outside", start_ms=11000, end_ms=12000),
    ], video(render_mode=mode))
    assert result["effective_render_mode"] == mode
    for key in ("excluded", "outside"):
        assert result["layouts"][key]["excluded"]
        assert "effective_render_mode" not in result["layouts"][key]


@pytest.mark.parametrize("category", ["product", "unboxing"])
def test_actual_29_second_three_lines_have_equal_measurement_in_both_modes(actual_fonts, category):
    # Input geometry and translation from the existing 29-second offline snapshot.
    lines = [
        ("headline", "180\u00b0 Flachlagerungs-Design", [65, 820, 325, 868], 0.038, 41),
        ("body", "Erreicht m\u00fchelos niedrige Bereiche, um verborgenen Staub zu entfernen.",
         [65, 873, 545, 913], 0.032, 34),
        ("footnote", "*Die H\u00f6he im flachliegenden Zustand betr\u00e4gt 12,75 cm",
         [65, 925, 200, 948], 0.018, 24),
    ]
    sources = [segment(segment_id=role, visual_role=role, translated_text=text,
                       bbox_normalized=box, font_height_ratio=ratio, alignment="left",
                       background_color="#33251B", start_ms=28500, end_ms=32000)
               for role, text, box, ratio, _ in lines]
    metadata = video(width=1920, height=1080, duration_ms=65400, fps=25,
                     video_category=category)
    png = layout.compile_subtitle_project(sources, metadata)
    native = layout.compile_subtitle_project(sources, {**metadata, "render_mode": "text"})
    assert len(png["layers"]) == 3 and not native["layers"]
    for role, text, _, _, size in lines:
        image_summary, text_summary = png["layouts"][role], native["layouts"][role]
        assert image_summary["effective_font_size_px"] == text_summary["effective_font_size_px"] == size
        for field in ("render_bbox_pixels", "target_time", "font_type", "font_sha256",
                      "text_color", "background_color", "alignment", "measured_text_width_px"):
            assert image_summary[field] == text_summary[field]
        assert image_summary["raster_scale"] == 4
        element = native["project"]["track"][text_summary["track_index"]][0]
        assert element["text"] == text and element["bold"] is (role != "headline")
        assert element["target_time"] == [28500, 32000]


@pytest.mark.parametrize("bad_output", ["layers", "image", "unresolved"])
def test_custom_text_compiler_rejected_without_fallback_materializer_or_submit(
    monkeypatch, bad_output,
):
    calls = []

    def compiler(segments, runtime):
        calls.append(runtime)
        element = {"type": "image" if bad_output == "image" else "text", "text": "Test"}
        if bad_output == "unresolved":
            element["source"] = "subtitle-layer:bad"
        return {"project": {"track": [[{"type": "video"}], [element]]},
                "layouts": {"line-1": {}}, "warnings": [],
                "layers": {"bad": {}} if bad_output == "layers" else {}}

    materializer = AsyncMock()
    prepare = AsyncMock()
    monkeypatch.setattr("video_subtitle_grounding.product_layout.materialize_layers", prepare)
    client = AsyncMock()
    service = SubtitleRenderService(client=client, compiler=compiler, layer_materializer=materializer)
    with pytest.raises(layout.SubtitleLayoutError, match="text_render_layers_unavailable"):
        asyncio.run(service.submit([segment()], video(render_mode="text"), idempotency_key="offline"))
    assert len(calls) == 1 and calls[0]["render_mode"] == "text"
    materializer.assert_not_called()
    prepare.assert_not_called()
    client.submit.assert_not_called()


def test_custom_png_fallback_receives_text_mode_and_retains_requested_png():
    calls = []

    def compiler(segments, runtime):
        calls.append(runtime["render_mode"])
        native = runtime["render_mode"] == "text"
        return {"project": {"track": [[{"type": "video"}],
                                     [{"type": "text" if native else "image", "text": "Test"}]]},
                "layouts": {"line-1": {}}, "warnings": [],
                "layers": {} if native else {"layer": {"id": "layer"}}}

    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="offline", status="queued")
    service = SubtitleRenderService(client=client, compiler=compiler)
    result = asyncio.run(service.submit([segment()], video(), idempotency_key="offline"))
    assert calls == ["png", "text"]
    assert result["render_mode"] == result["layouts"]["line-1"]["render_mode"] == "png"
    assert result["effective_render_mode"] == result["layouts"]["line-1"]["effective_render_mode"] == "text"
    assert client.submit.call_args.kwargs["project"]["output"]["codec"] == {
        "video_codec": "h264", "crf": 16}


@pytest.mark.parametrize("requested,effective", [("png", "png"), ("png", "text"), ("text", "text")])
def test_submitted_event_and_logging_whitelist_include_safe_render_contract(
    font_http, caplog, requested, effective,
):
    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="offline", status="queued")
    materializer = AsyncMock(return_value="https://assets.example.com/layer.png") if effective == "png" else None
    service = SubtitleRenderService(client=client, layer_materializer=materializer)
    with caplog.at_level(logging.INFO, logger="video_subtitle_grounding.render"):
        result = asyncio.run(service.submit([segment()], video(render_mode=requested),
                                            idempotency_key="offline"))
    record = next(r for r in caplog.records if r.message == "subtitle_translation.submitted")
    context = json.loads(JsonFormatter().format(record))
    expected = {"render_mode": requested, "effective_render_mode": effective,
                "video_codec": "h264", "crf": 16, "encoding_policy_version": "configurable-crf-h264-v3"}
    assert {key: context[key] for key in expected} == expected
    assert {key: result[key] for key in expected} == expected
    assert "signature=private" not in JsonFormatter().format(record)

    sink = SimpleNamespace(emit=lambda event: events.append(event))
    events = []
    tls_only_logger(sink)("render.safe", outcome="succeeded", output_text="", **expected,
                          source_url="https://assets.example.com/?signature=private", api_key="private")
    assert {key: events[0][key] for key in expected} == expected
    assert "source_url" not in events[0] and "api_key" not in events[0]
    with caplog.at_level(logging.INFO, logger="video_subtitle_grounding.render"):
        log_event(logging.getLogger("video_subtitle_grounding.render"), "render.safe",
                  outcome="succeeded", **expected, source_url="private", api_key="private")
    safe = json.loads(JsonFormatter().format(caplog.records[-1]))
    assert {key: safe[key] for key in expected} == expected
    assert "source_url" not in safe and "api_key" not in safe
