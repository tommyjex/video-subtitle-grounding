"""Strict CRF and final wire policy, including injected compilers and fallbacks."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from video_subtitle_grounding import layout
from video_subtitle_grounding.encoding import apply_subtitle_output_policy, frozen_crf, validate_crf
from video_subtitle_grounding.mediakit import MultiTrackTask
from video_subtitle_grounding.render import SubtitleRenderService
from video_subtitle_grounding.translation_schemas import SubtitleRenderRequest
from test_layout import font_http, segment, video
from test_render_api import KEY, app_for

__all__ = ["font_http"]


@pytest.mark.parametrize("value", [-1, 31, 12.0, 12.5, "12", False, True, None])
def test_strict_invalid_crf_never_reaches_provider(value):
    with pytest.raises(ValueError, match="invalid_crf"):
        validate_crf(value)
    body = {"segments": [segment()], "video": video(), "idempotency_key": "offline", "crf": value}
    with pytest.raises(ValidationError):
        SubtitleRenderRequest.model_validate(body)
    client = AsyncMock()
    renderer = SubtitleRenderService(client=client)
    with TestClient(app_for(renderer=renderer)) as api:
        assert api.post("/v1/subtitles/render", json=body, headers=KEY).status_code == 422
    client.submit.assert_not_called()
    with pytest.raises(layout.SubtitleLayoutError, match="invalid_crf"):
        layout.compile_subtitle_project([segment()], video(crf=value))
    with pytest.raises(layout.SubtitleLayoutError, match="invalid_crf"):
        asyncio.run(renderer.submit([segment()], video(crf=value), idempotency_key="offline"))


@pytest.mark.parametrize("crf", [0, 12, 16, 30])
@pytest.mark.parametrize("mode", ["png", "text"])
@pytest.mark.parametrize("category", ["product", "unboxing"])
@pytest.mark.parametrize("panel", [None, "#FF000080"])
def test_final_policy_keeps_raster_pixels_and_identity(font_http, crf, mode, category, panel):
    source = segment(background_color="#777777",
                     style={} if panel is None else {"background_color": panel})
    runtime = video(render_mode=mode, video_category=category)
    baseline = layout.compile_subtitle_project([source], runtime)
    compiled = layout.compile_subtitle_project([source], {**runtime, "crf": crf})
    assert compiled["layouts"] == baseline["layouts"]
    assert compiled["layers"] == baseline["layers"]
    project = compiled["project"]
    assert project["output"]["codec"] == {"video_codec": "h264", "crf": crf}
    assert "background_color" not in project["canvas"]
    assert all("background_color" not in element for track in project["track"] for element in track)
    assert compiled["layouts"]["line-1"]["background_color"] == (
        "#00000000" if category == "product" else panel or "#33333359")


@pytest.mark.parametrize("crf", [0, 12, 16, 30])
@pytest.mark.parametrize("mode", ["png", "text"])
def test_actual_submit_and_storage_fallback_use_selected_crf(font_http, crf, mode):
    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="offline", status="queued")
    events = []
    renderer = SubtitleRenderService(
        client=client, layer_materializer=AsyncMock(side_effect=OSError("offline")),
        event_logger=lambda *args, **kwargs: events.append(kwargs))
    result = asyncio.run(renderer.submit([segment()], video(crf=crf, render_mode=mode),
                                         idempotency_key="offline"))
    project = client.submit.call_args.kwargs["project"]
    assert project["output"]["codec"] == {"video_codec": "h264", "crf": crf}
    assert "background_color" not in project["canvas"]
    assert all("background_color" not in element for track in project["track"] for element in track)
    assert result["effective_render_mode"] == "text"
    assert result["crf"] == events[0]["crf"] == crf


@pytest.mark.parametrize("crf", [None, 0, 12, 16, 30])
def test_api_top_level_is_authoritative_even_for_injected_compiler(crf):
    seen = []
    def compiler(segments, runtime):
        seen.append(runtime)
        return {"project": {"canvas": {"background_color": "#000000FF"},
                            "track": [[{"type": "video"}], [{"type": "text", "background_color": "#FF0000FF"}]],
                            "output": {"codec": {"video_codec": "h265", "video_bit_rate": 7000}}},
                "layouts": {}, "warnings": []}
    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="offline", status="queued")
    renderer = SubtitleRenderService(client=client, compiler=compiler)
    body = {"segments": [segment()], "video": video(crf=30), "render_mode": "text", "idempotency_key": "offline"}
    if crf is not None:
        body["crf"] = crf
    with TestClient(app_for(renderer=renderer)) as api:
        result = api.post("/v1/subtitles/render", json=body, headers=KEY)
    expected = 16 if crf is None else crf
    assert result.status_code == 200 and result.json()["crf"] == expected
    assert seen[0]["crf"] == expected
    assert client.submit.call_args.kwargs["project"]["output"]["codec"] == {"video_codec": "h264", "crf": expected}


def test_policy_is_idempotent_and_only_removes_wire_backgrounds():
    project = {"canvas": {"width": 1280, "background_color": "#000000FF"},
               "track": [[{"type": "text", "background_color": "#33333359", "font_color": "#FFFFFFFF"}],
                         [{"type": "image", "source": "layer", "background_color": "keep"}]],
               "output": {"codec": {"preset": "medium", "audio_codec": "aac", "video_bit_rate": 7000}}}
    assert apply_subtitle_output_policy(project, 0) is project
    once = deepcopy(project)
    assert apply_subtitle_output_policy(project, 0) == once
    assert project["track"][1][0]["background_color"] == "keep"
    assert project["output"]["codec"] == {"preset": "medium", "audio_codec": "aac", "video_codec": "h264", "crf": 0}


@pytest.mark.parametrize("snapshot,expected", [
    ({}, 16), ({"crf": 0}, 0), ({"crf": 12, "encoding_result": {"crf": 30}}, 12),
    ({"crf": None, "encoding_result": {"crf": 0}}, 0),
    ({"encoding_result": {"crf": 30}}, 30),
    ({"crf": False, "encoding_result": {"crf": "12"}}, 16),
])
def test_legacy_crf_uses_only_frozen_values(snapshot, expected):
    assert frozen_crf(snapshot) == expected
