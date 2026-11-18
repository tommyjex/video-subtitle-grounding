"""Offline end-to-end boundary checks for translation and rendering."""

import asyncio
import io
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from video_subtitle_grounding.api import create_app
from video_subtitle_grounding.config import Settings
from video_subtitle_grounding.layout import SubtitleLayoutError
from video_subtitle_grounding.logging import JsonFormatter
from video_subtitle_grounding.mediakit import MediaKitMultiTrackClient
from video_subtitle_grounding.render import SubtitleRenderService
from video_subtitle_grounding.translation import ModelArkSubtitleTranslationAdapter
from test_layout import font_http, segment, video
from test_product_layout import product_segment

__all__ = ["font_http"]

KEY = {"X-API-Key": "test-key"}
RECORD = {"segment_id": "one", "text": "Source", "block_id": "block", "line_index": 0}


class Translator:
    def __init__(self):
        self.calls = []

    async def translate(self, segments, **kwargs):
        self.calls.append((segments, kwargs))
        return {"translations": {"one": "Translated"}, "failed_ids": [],
                "usage": {"total_tokens": 1}, "model": "fixture"}


def app_for(*, translator=None, renderer=None):
    # Extraction injected to avoid creating any real SDK clients.
    return create_app(Settings(service_api_key="test-key"),
                      service=SimpleNamespace(), translator=translator, renderer=renderer)


def test_translation_auth_defaults_and_selected_ids():
    translator = Translator()
    with TestClient(app_for(translator=translator)) as client:
        body = {"segments": [RECORD], "target_language": "de"}
        assert client.post("/v1/subtitles/translate", json=body).status_code == 401
        response = client.post("/v1/subtitles/translate", json=body, headers=KEY)
        assert response.json()["translations"] == {"one": "Translated"}
        assert translator.calls[0][1]["segment_ids"] == ["one"]
        assert translator.calls[0][1]["source_language"] == "auto"
        body["segment_ids"] = []
        assert client.post("/v1/subtitles/translate", json=body, headers=KEY).status_code == 200
        assert translator.calls[-1][1]["segment_ids"] == []


@pytest.mark.parametrize("body", [
    {"segments": [RECORD, RECORD], "target_language": "de"},
    {"segments": [RECORD], "segment_ids": ["missing"], "target_language": "de"},
    {"segments": [RECORD], "segment_ids": ["one", "one"], "target_language": "de"},
])
def test_translation_invalid_ids_cannot_reach_provider(body):
    translator = Translator()
    with TestClient(app_for(translator=translator)) as client:
        assert client.post("/v1/subtitles/translate", json=body, headers=KEY).status_code == 422
    assert translator.calls == []


def test_translation_capacity_and_missing_configuration():
    translator = Translator()
    app = app_for(translator=translator)
    with TestClient(app) as client:
        client.portal.call(app.state.translation_slots.acquire)
        response = client.post("/v1/subtitles/translate", headers=KEY, json={
            "segments": [RECORD], "target_language": "de",
        })
        assert response.status_code == 429
        client.portal.call(app.state.translation_slots.release)
    assert translator.calls == []
    with TestClient(app_for()) as client:
        assert client.post("/v1/subtitles/translate", headers=KEY, json={
            "segments": [RECORD], "target_language": "de",
        }).status_code == 503
        assert client.get("/v1/subtitles/render/task", headers=KEY).status_code == 503


def test_product_recognition_auth_capacity_ids_and_no_translation():
    from unittest.mock import AsyncMock

    translator = AsyncMock()
    translator.recognize_products.return_value = {"product_mentions": {"one": []}}
    app = app_for(translator=translator)
    body = {"segments": [RECORD]}
    with TestClient(app) as client:
        url = "/v1/subtitles/recognize-products"
        assert client.post(url, json=body).status_code == 401
        assert client.post(url, json={**body, "segment_ids": ["missing"]}, headers=KEY).status_code == 422
        client.portal.call(app.state.translation_slots.acquire)
        assert client.post(url, json=body, headers=KEY).status_code == 429
        client.portal.call(app.state.translation_slots.release)
        assert client.post(url, json=body, headers=KEY).json() == {"product_mentions": {"one": []}}
    translator.recognize_products.assert_awaited_once_with([RECORD], segment_ids=["one"])
    translator.translate.assert_not_called()
    with TestClient(app_for()) as client:
        assert client.post(url, json=body, headers=KEY).status_code == 503


def test_render_submit_and_poll_wire_contract_without_signed_source_in_response():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"success": True, "task_id": "task-1"})
        return httpx.Response(200, json={
            "success": True, "status": "completed", "task_id": "task-1",
            "result": {"video_url": "https://assets.example/output.mp4"},
        })

    def compiler(segments, video):
        assert segments[0]["segment_id"] == "one"
        return {
            "project": {"canvas": {"width": 1280, "height": 720},
                        "track": [[{"type": "video", "source": video["url"]}]],
                        "output": {"format": "mp4"}},
            "layouts": {"one": {"effective_font_size_px": 20}}, "warnings": [],
        }

    renderer = SubtitleRenderService(
        client=MediaKitMultiTrackClient(Settings(mediakit_api_key="fixture-key"),
                                       transport=httpx.MockTransport(handler)),
        compiler=compiler,
    )
    app = app_for(renderer=renderer)
    with TestClient(app) as client:
        body = {"segments": [RECORD], "video": {
            "url": "https://example.com/clean.mp4?signature=PRIVATE",
        }, "idempotency_key": "render-v1"}
        assert client.post("/v1/subtitles/render", json=body).status_code == 401
        response = client.post("/v1/subtitles/render", json=body, headers=KEY)
        assert response.status_code == 200
        assert "PRIVATE" not in response.text
        assert response.json()["task"]["task_id"] == "task-1"
        assert response.json()["layouts"]["one"]["effective_font_size_px"] == 20
        state = client.get("/v1/subtitles/render/task-1", headers=KEY)
        assert state.json()["status"] == "succeeded"
        assert state.json()["output_video_url"] == "https://assets.example/output.mp4"
        client.portal.call(app.state.render_slots.acquire)
        assert client.post("/v1/subtitles/render", json=body, headers=KEY).status_code == 429
        client.portal.call(app.state.render_slots.release)
        assert client.get("/v1/subtitles/render/invalid!", headers=KEY).status_code == 422
    payload = json.loads(requests[0].content)
    assert payload["client_token"] == renderer.client.build_client_token("render-v1")
    assert asyncio.run(renderer.poll(task_id="task-1"))["status"] == "succeeded"


def test_layout_failure_is_redacted_and_never_submits():
    def compiler(*args):
        raise SubtitleLayoutError("invalid_bbox", "one")

    renderer = SubtitleRenderService(client=SimpleNamespace(), compiler=compiler)
    with TestClient(app_for(renderer=renderer)) as client:
        response = client.post("/v1/subtitles/render", headers=KEY, json={
            "segments": [RECORD], "video": {"url": "PRIVATE_URL"}, "idempotency_key": "one",
        })
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_bbox"
    assert "PRIVATE_URL" not in response.text


def test_provider_failure_cannot_expose_key_or_response_body():
    renderer = SubtitleRenderService(client=MediaKitMultiTrackClient(
        Settings(mediakit_api_key="PRIVATE_KEY"),
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="PRIVATE_RAW")),
    ))
    with TestClient(app_for(renderer=renderer)) as client:
        response = client.get("/v1/subtitles/render/task", headers=KEY)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "http_error"
    assert "PRIVATE" not in response.text


def test_translation_raw_output_only_reaches_injected_tls_hook():
    events = []
    output = '{"translations":[{"segment_id":"one","translated_text":"PRIVATE_RAW"}]}'
    client = SimpleNamespace(responses=SimpleNamespace(with_raw_response=SimpleNamespace(
        create=lambda **kwargs: {"output": [{"content": [{"type": "output_text", "text": output}]}]},
    )))
    logger = logging.getLogger("video_subtitle_grounding.translation")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        translator = ModelArkSubtitleTranslationAdapter(
            Settings(), client=client,
            tls_only_logger=lambda *args, **kwargs: events.append(kwargs),
        )
        result = asyncio.run(translator.translate(
            [RECORD], segment_ids=["one"], source_language="en", target_language="de",
        ))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    assert result["translations"]["one"] == "PRIVATE_RAW"
    assert "PRIVATE_RAW" not in stream.getvalue()
    assert events[0]["output_text"] == output


def test_owned_translation_client_closes_but_injected_client_is_caller_owned(monkeypatch):
    class Owned:
        closed = False
        client = SimpleNamespace(close=lambda: setattr(Owned, "closed", True))

        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr("video_subtitle_grounding.api.ModelArkSubtitleTranslationAdapter", Owned)
    with TestClient(create_app(Settings(service_api_key="test-key", ark_api_key="fixture"),
                               service=SimpleNamespace())):
        pass
    assert Owned.closed
    Owned.closed = False
    with TestClient(app_for(translator=Owned())):
        pass
    assert not Owned.closed


@pytest.mark.parametrize("category", ["product", "unboxing", None])
@pytest.mark.parametrize("failure", ["generation", "font", "no_materializer", "storage", "invalid_source"])
def test_render_api_fallback_keeps_category_policy(font_http, monkeypatch, category, failure):
    from copy import deepcopy
    from unittest.mock import AsyncMock

    from video_subtitle_grounding import layout, product_layout
    from video_subtitle_grounding.mediakit import MultiTrackTask

    if failure == "generation":
        def failed(*args):
            raise SubtitleLayoutError("product_layout_unavailable")
        monkeypatch.setattr(product_layout, "compile_product_layout", failed)
    elif failure == "font":
        original = layout._load_font_resource
        def failed_font(url):
            if url == layout.FONT_RESOURCES["AvenirLTStdBlack"]:
                raise SubtitleLayoutError("font_resource_unavailable")
            return original(url)
        monkeypatch.setattr(layout, "_load_font_resource", failed_font)

    async def materializer(layer):
        if failure == "storage":
            raise OSError("PRIVATE_STORAGE_ERROR")
        return "http://invalid.example/layer.png"

    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="fixture", status="queued")
    renderer = SubtitleRenderService(
        client=client, layer_materializer=None if failure == "no_materializer" else materializer)
    sources = [
        {**product_segment(), "background_color": "#FFFFFF",
         "style": {"background_color": "#FF0000FF"}},
        segment(segment_id="body", visual_role="body", background_color=None),
        segment(segment_id="footnote", visual_role="footnote", background_color="#777777",
                style={"text_color": "#00FF00", "background_color": "#FFFFFF80"}),
    ]
    metadata = video(video_bit_rate=7000, **({"video_category": category} if category is not None else {}))
    before = deepcopy((sources, metadata))
    with TestClient(app_for(renderer=renderer)) as api:
        response = api.post("/v1/subtitles/render", headers=KEY, json={
            "segments": sources, "video": metadata, "idempotency_key": "offline-fallback"})
    assert response.status_code == 200
    assert (sources, metadata) == before
    assert "PRIVATE_STORAGE_ERROR" not in response.text
    project = client.submit.call_args.kwargs["project"]
    assert project["output"]["codec"] == {"video_codec": "h264", "crf": 16}
    elements = [element for track in project["track"][1:] for element in track]
    assert all(element["type"] == "text" for element in elements)
    assert [element["bold"] for element in elements] == [False, True, True]
    assert not any("subtitle-layer:" in str(element) for element in elements)
    assert all("background_color" not in element for element in elements)
    assert "background_color" not in project["canvas"]
    assert [element["font_color"] for element in elements] == (
        ["#000000FF", "#FFFFFFFF", "#00FF00FF"] if category == "product" else
        ["#FFFFFFFF", "#FFFFFFFF", "#00FF00FF"])
    assert elements[0]["font_type"] == layout.CENTURY_GOTHIC_BOLD_URL
    assert all(element["target_time"] == [100, 1000] for element in elements)
    layouts = response.json()["layouts"]
    assert [layouts[source["segment_id"]]["background_color"] for source in sources] == (
        ["#00000000"] * 3 if category == "product" else
        ["#FF0000FF", "#33333359", "#FFFFFF80"])
    expected_warning = ("product_layout_unavailable" if failure in ("generation", "font")
                        else "product_layer_materialization_unavailable")
    assert expected_warning in layouts["line-1"]["warnings"]


@pytest.mark.parametrize("category", ["product", "unboxing", None])
@pytest.mark.parametrize("role", ["headline", "body", "footnote"])
def test_render_api_materialized_mixed_and_ordinary_png_share_policy(font_http, category, role):
    from io import BytesIO
    from unittest.mock import AsyncMock

    from PIL import Image

    from video_subtitle_grounding.mediakit import MultiTrackTask

    uploaded = []

    async def materializer(layer):
        uploaded.append(layer)
        return "https://assets.example/layer.png?signature=PRIVATE_LAYER"

    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="fixture", status="queued")
    renderer = SubtitleRenderService(client=client, layer_materializer=materializer)
    sources = [
        {**product_segment(), "visual_role": role, "background_color": None,
         "style": {"background_color": "#FF0000FF"}},
        segment(segment_id="plain", background_color=None),
    ]
    metadata = video(video_bit_rate=7000000, **({"video_category": category} if category is not None else {}))
    with TestClient(app_for(renderer=renderer)) as api:
        response = api.post("/v1/subtitles/render", headers=KEY, json={
            "segments": sources, "video": metadata, "idempotency_key": "offline-png"})
    assert response.status_code == 200
    assert len(uploaded) == 2
    assert "PRIVATE_LAYER" not in response.text
    project = client.submit.call_args.kwargs["project"]
    assert project["output"]["codec"] == {"video_codec": "h264", "crf": 16}
    image, text = [element for track in project["track"][1:] for element in track]
    assert image["type"] == "image"
    assert "bold" not in image
    assert text["type"] == "image" and "bold" not in text
    assert image["source"] == "https://assets.example/layer.png?signature=PRIVATE_LAYER"
    assert response.json()["layouts"]["plain"]["background_color"] == (
        "#00000000" if category == "product" else "#33333359")
    assert response.json()["layouts"]["line-1"]["background_color"] == (
        "#00000000" if category == "product" else "#FF0000FF")
    png = Image.open(BytesIO(uploaded[0]["content"]))
    colors = {color for _, color in png.getcolors(png.width * png.height)}
    assert ((255, 0, 0, 255) in colors) is (category != "product")
    assert "product_layer_materialization_unavailable" not in response.json()["warnings"]


@pytest.mark.parametrize("category", ["product", "unboxing", None])
@pytest.mark.parametrize("role", ["headline", "body", "footnote", None, "legacy", "absent"])
def test_render_api_submitted_bold_follows_role_without_changing_codec(font_http, category, role):
    from unittest.mock import AsyncMock
    from video_subtitle_grounding.mediakit import MultiTrackTask

    source = segment(visual_role=role)
    if role == "absent":
        source.pop("visual_role")
    client = AsyncMock()
    client.submit.return_value = MultiTrackTask(task_id="fixture", status="queued")
    renderer = SubtitleRenderService(client=client)
    metadata = video(video_bit_rate=7000, **(
        {"video_category": category} if category is not None else {}))
    with TestClient(app_for(renderer=renderer)) as api:
        response = api.post("/v1/subtitles/render", headers=KEY, json={
            "segments": [source], "video": metadata, "idempotency_key": "offline-bold"})
    assert response.status_code == 200
    project = client.submit.call_args.kwargs["project"]
    element = project["track"][1][0]
    assert element["bold"] is (role != "headline")
    assert element["text"] == source["translated_text"]
    assert element["target_time"] == [source["start_ms"], source["end_ms"]]
    assert element["line_max_width"] == -1
    assert project["output"]["codec"] == {"video_codec": "h264", "crf": 16}
