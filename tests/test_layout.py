"""Offline compiler tests: real FreeType metrics, mocked font HTTP, no paid APIs."""

import hashlib
import json
import struct
import tomllib
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from PIL import ImageFont

from video_subtitle_grounding import layout
from video_subtitle_grounding.mediakit import MediaKitMultiTrackClient

FONT_PATH = Path(__file__).parent / "fixtures/subtitle_fonts/ABeeZee-Regular.ttf"
HTTP_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def native_wire_contract(monkeypatch):
    """This suite verifies the explicit native-text fallback wire contract.

    Default PNG behavior and sizing are covered separately in test_raster.py.
    """
    compiler = layout.compile_subtitle_project
    monkeypatch.setattr(layout, "compile_subtitle_project",
                        lambda segments, video, **kwargs: compiler(segments, video, rasterize=False))


@pytest.fixture(autouse=True)
def font_http(monkeypatch):
    # The font resource HTTP body is deterministic and measured by real Pillow.
    # This fixture is OFL licensed; production never uses it as an approximation.
    content = FONT_PATH.read_bytes()
    original_client = HTTP_CLIENT
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=content)

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(respond))

    original_loader = layout._load_font_resource
    original_loader.cache_clear()
    monkeypatch.setattr(layout.httpx, "Client", client)
    yield requests
    original_loader.cache_clear()


def video(**overrides):
    return {
        "width": 1280,
        "height": 720,
        "duration_ms": 10_000,
        "source_url": "https://assets.example.com/clean.mp4?signature=private",
        "fps": 30,
        "rotation": 0,
        "has_audio": True,
        **overrides,
    }


@pytest.mark.parametrize("category", ["product", "unboxing"])
@pytest.mark.parametrize("value", [7000, "7000000", None, "N/A", True, 7000.1])
def test_crf_output_ignores_source_bitrate(category, value):
    result = layout.compile_subtitle_project(
        [segment()], video(video_category=category, video_bit_rate=value))
    output = result["project"]["output"]
    assert output["disable_audio"] is False and output["fps"] == 30
    assert output["codec"] == {"video_codec": "h264", "crf": 16}
    assert "source_video_bit_rate_unavailable" not in result["warnings"]
    assert "video_bit_rate" not in output


def segment(**overrides):
    return {
        "segment_id": "line-1",
        "text": "Source",
        "translated_text": "Übermäßig längere Übersetzung",
        "start_ms": 100,
        "end_ms": 1000,
        "bbox_normalized": [100, 200, 500, 300],
        "alignment": "center",
        "background_color": "#FFFFFF",
        "style": {},
        **overrides,
    }


def compile_one(**overrides):
    return layout.compile_subtitle_project([segment(**overrides)], video())


def test_real_metrics_fit_height_then_full_german_width(font_http):
    result = compile_one()
    summary = result["layouts"]["line-1"]
    element = result["project"]["track"][1][0]
    box = summary["render_bbox_pixels"]
    font = ImageFont.truetype(str(FONT_PATH), summary["effective_font_size_px"])
    estimated_font = ImageFont.truetype(
        str(FONT_PATH), summary["estimated_font_size_px"]
    )
    assert sum(estimated_font.getmetrics()) <= box[3] - box[1]
    assert (
        sum(ImageFont.truetype(str(FONT_PATH), estimated_font.size + 1).getmetrics())
        > box[3] - box[1]
    )
    assert layout._text_width(font, element["text"]) <= box[2] - box[0]
    assert summary["effective_font_size_px"] < summary["estimated_font_size_px"]
    assert "font_size_reduced" in summary["warnings"]
    assert element["text"] == segment()["translated_text"]
    assert not summary["missing_glyphs"]
    assert element["font_type"] == font_http[0] == layout.CENTURY_GOTHIC_URL
    assert summary["font_sha256"] == hashlib.sha256(FONT_PATH.read_bytes()).hexdigest()
    assert element["line_max_width"] == -1
    assert element["typesetting"] == 0
    assert element["font_size"] == summary["effective_font_size_px"]
    assert element["extra"] == [
        {
            "type": "transform",
            "pos_x": box[0],
            "pos_y": box[1],
            "width": box[2] - box[0],
            "height": box[3] - box[1],
            "rotation": 0,
        }
    ]
    assert "transform" not in element and "style" not in element


@pytest.mark.parametrize("alignment,wire", [("left", 0), ("center", 1), ("right", 2)])
def test_alignment_preserved_without_scaling(alignment, wire):
    result = compile_one(alignment=alignment, translated_text="Hi")
    element = result["project"]["track"][1][0]
    summary = result["layouts"]["line-1"]
    assert element["align_type"] == wire
    assert summary["alignment"] == alignment
    assert summary["effective_font_size_px"] == summary["estimated_font_size_px"]
    assert len(element["extra"]) == 1


def test_missing_alignment_and_invalid_alignment():
    assert compile_one(alignment=None)["project"]["track"][1][0]["align_type"] == 1
    assert (
        "alignment_defaulted"
        in compile_one(alignment="bogus")["layouts"]["line-1"]["warnings"]
    )


@pytest.mark.parametrize(
    "background,expected,panel",
    [
        ("#FFFFFF", "#000000", "#00000000"),
        ("#F5F5F5", "#000000", "#00000000"),
        ("#F4F5F5", "#FFFFFF", "#33333359"),
        ("#000000", "#FFFFFF", "#00000000"),
        ("#101010", "#FFFFFF", "#00000000"),
        ("#111010", "#FFFFFF", "#33333359"),
        ("#777777", "#FFFFFF", "#33333359"),
        ("#0000FF", "#FFFFFF", "#33333359"),
        ("#FF0000", "#FFFFFF", "#33333359"),
    ],
)
def test_background_policy_selects_plain_text_or_gray_panel(
    background, expected, panel
):
    result = compile_one(background_color=background)
    summary = result["layouts"]["line-1"]
    assert summary["recommended_text_color"] == expected
    assert summary["background_color"] == panel
    assert result["project"]["track"][1][0]["font_color"] == expected + "FF"
    assert "background_color" not in result["project"]["track"][1][0]
    assert "border_color" not in result["project"]["track"][1][0]
    assert "border_width" not in result["project"]["track"][1][0]


@pytest.mark.parametrize("background", [None, "invalid", "#FFFFFF99"])
def test_unknown_background_is_white_with_translucent_gray(background):
    result = compile_one(background_color=background)
    element = result["project"]["track"][1][0]
    assert element["font_color"] == "#FFFFFFFF"
    assert "background_color" not in element
    assert result["layouts"]["line-1"]["background_color"] == "#33333359"
    assert "background_unknown" in result["layouts"]["line-1"]["warnings"]


def test_explicit_panel_uses_white_text_and_can_be_disabled():
    result = compile_one(style={"background_color": "#333333B3"})
    assert result["project"]["track"][1][0]["font_color"] == "#FFFFFFFF"
    assert "low_contrast" not in result["layouts"]["line-1"]["warnings"]
    result = compile_one(
        background_color="#888888", style={"background_color": "#00000000"}
    )
    assert "background_color" not in result["project"]["track"][1][0]


def test_user_overrides_and_low_contrast():
    result = compile_one(
        style={
            "font_size_px": 18,
            "font": "CenturyGothic",
            "text_color": "#FFFFFF",
            "background_color": "#FFFFFF80",
            "alignment": "right",
        }
    )
    element = result["project"]["track"][1][0]
    summary = result["layouts"]["line-1"]
    assert element["font_type"] == layout.CENTURY_GOTHIC_URL
    assert element["font_size"] <= 18
    assert element["font_color"] == "#FFFFFFFF"
    assert "background_color" not in element
    assert summary["background_color"] == "#FFFFFF80"
    assert element["align_type"] == 2
    assert "low_contrast" in summary["warnings"]
    assert "font_url_version_unverified" in summary["warnings"]
    assert summary["recommended_text_color"] == "#000000"


def test_panel_alpha_composites_before_contrast():
    result = compile_one(
        style={"text_color": "#FFFFFF", "background_color": "#000000FF"}
    )
    assert "low_contrast" not in result["layouts"]["line-1"]["warnings"]
    result = compile_one(style={"text_color": "#FFFFFF00"})
    assert "low_contrast" in result["layouts"]["line-1"]["warnings"]


@pytest.mark.parametrize(
    "style,code",
    [
        ({"font": "1187225"}, "font_resource_unavailable"),
        ({"font": "https://private.invalid/font.ttf"}, "font_resource_unavailable"),
        ({"font_size_px": 0}, "invalid_font_size"),
        ({"font_size_px": float("nan")}, "invalid_font_size"),
        ({"text_color": "red"}, "invalid_text_color"),
        ({"background_color": "red"}, "invalid_panel_color"),
    ],
)
def test_invalid_overrides_fail_without_guessing(style, code):
    with pytest.raises(layout.SubtitleLayoutError) as exc:
        compile_one(style=style)
    assert exc.value.code == code
    assert exc.value.segment_id == "line-1"
    assert "private.invalid" not in str(exc.value)


def test_missing_glyphs_use_provider_preset_without_external_fallback(font_http):
    result = compile_one(translated_text="Missing 漢字")
    summary = result["layouts"]["line-1"]
    assert "missing_glyphs" in summary["warnings"]
    assert summary["missing_glyphs"] == ["U+5B57", "U+6F22"]
    assert summary["fallback_fonts"] == ["ALi_PuHui"]
    assert summary["font_type"] == "ALi_PuHui"
    assert summary["font_sha256"] is None
    assert "font_metrics_unverified" in summary["warnings"]
    assert "preset_glyph_coverage_unverified" in summary["warnings"]
    assert font_http == [layout.CENTURY_GOTHIC_URL]
    assert result["project"]["track"][1][0]["font_type"] == "ALi_PuHui"
    assert result["project"]["track"][1][0]["text"] == "Missing 漢字"


def test_preset_fallback_keeps_measurement_resource_explicit(monkeypatch, font_http):
    original = layout._load_font_resource

    def resource(url):
        found = original(url)
        if url == layout.CENTURY_GOTHIC_URL:
            # Simulate a provider font lacking German glyph coverage.
            return layout._FontResource(
                found.url, found.content, found.sha256, found.codepoints - {ord("Ü")}
            )
        return found

    monkeypatch.setattr(layout, "_load_font_resource", resource)
    result = compile_one(style={"font": "CenturyGothic"})
    summary = result["layouts"]["line-1"]
    assert "missing_glyphs" in summary["warnings"]
    assert "font_fallback" in summary["warnings"]
    assert summary["missing_glyphs"] == ["U+00DC"]
    assert summary["font_type"] == "ALi_PuHui"
    assert summary["font_sha256"] is None
    assert summary["measurement_font_type"] == layout.CENTURY_GOTHIC_URL
    assert (
        summary["measurement_font_sha256"]
        == hashlib.sha256(FONT_PATH.read_bytes()).hexdigest()
    )
    assert font_http == [layout.CENTURY_GOTHIC_URL]
    assert result["project"]["track"][1][0]["font_type"] == summary["font_type"]


def test_celsius_badcase_keeps_literal_text_and_omits_stroke(font_http):
    result = compile_one(translated_text="Temperatura 60℃")
    element = result["project"]["track"][1][0]
    payload = MediaKitMultiTrackClient._normalize_submit_payload(
        project=result["project"], idempotency_key="celsius-preset-regression"
    )
    assert element["font_type"] == "ALi_PuHui"
    assert element["text"] == "Temperatura 60℃"
    assert payload["track"][1][0] == element
    assert "border_color" not in element and "border_width" not in element
    assert font_http == [layout.CENTURY_GOTHIC_URL]


def test_default_regular_font_does_not_guess_role_from_language():
    result = layout.compile_subtitle_project([segment()], video(target_language="ja"))
    assert result["layouts"]["line-1"]["font_type"] == layout.CENTURY_GOTHIC_URL
    assert "font_url_version_unverified" in result["layouts"]["line-1"]["warnings"]


def test_automatic_roles_are_resolved_per_line_in_same_block(font_http):
    roles = ["headline", "body", "footnote", None, "legacy", 123, ["headline"]]
    sources = [
        segment(
            segment_id=f"role-{index}",
            visual_role=role,
            block_id="shared",
            line_index=index,
            bbox_normalized=[400, 50 + index * 100, 600, 130 + index * 100],
            start_ms=100 + index,
            end_ms=1000 + index,
            alignment=["left", "center", "right"][index % 3],
            background_color=["#FFFFFF", "#000000", "#777777"][index % 3],
            translated_text="Long translated title",
            font_type=layout.CENTURY_GOTHIC_BOLD_URL,
        )
        for index, role in enumerate(roles)
    ]
    sources.append(
        segment(
            segment_id="missing-role",
            block_id="shared",
            line_index=len(roles),
            start_ms=200,
            end_ms=1100,
            bbox_normalized=[400, 750, 600, 830],
            translated_text="Long translated title",
        )
    )
    before = deepcopy(sources)
    result = layout.compile_subtitle_project(sources, video())
    assert sources == before
    elements = {
        tuple(element["target_time"]): element
        for track in result["project"]["track"][1:]
        for element in track
    }
    for source in sources:
        summary = result["layouts"][source["segment_id"]]
        element = elements[tuple(summary["target_time"])]
        expected = (
            layout.CENTURY_GOTHIC_BOLD_URL
            if source.get("visual_role") == "headline"
            else layout.CENTURY_GOTHIC_URL
        )
        assert element["font_type"] == summary["measurement_font_type"] == expected
        assert summary["font_type"] == expected
        assert element["bold"] is (source.get("visual_role") != "headline")
        assert element["text"] == source["translated_text"]
        assert element["line_max_width"] == -1
        assert element["target_time"] == [source["start_ms"], source["end_ms"]]
        assert summary["alignment"] == source["alignment"]
        assert element["align_type"] == layout._ALIGNMENT[source["alignment"]]
        assert "background_color" not in element
        assert summary["background_color"] == {
            "#FFFFFF": "#00000000",
            "#000000": "#00000000",
            "#777777": "#33333359",
        }[source["background_color"]]
        original, rendered = (
            summary["original_bbox_pixels"],
            summary["render_bbox_pixels"],
        )
        assert original == list(layout._bbox(source, 1280, 720))
        assert rendered[1::2] == original[1::2]
        assert rendered[2] - rendered[0] >= original[2] - original[0]
        assert summary["measured_text_width_px"] <= rendered[2] - rendered[0]
        if rendered != original:
            assert "expanded_background_unverified" in summary["warnings"]
    assert font_http == [layout.CENTURY_GOTHIC_BOLD_URL, layout.CENTURY_GOTHIC_URL]


@pytest.mark.parametrize("role", ["headline", "body", "footnote", None, "legacy"])
@pytest.mark.parametrize("style", [{}, {"font": None}, {"font": ""}])
def test_empty_manual_selection_restores_role_default(role, style):
    result = compile_one(visual_role=role, style=style)
    expected = (
        layout.CENTURY_GOTHIC_BOLD_URL
        if role == "headline"
        else layout.CENTURY_GOTHIC_URL
    )
    assert result["layouts"]["line-1"]["measurement_font_type"] == expected
    assert result["project"]["track"][1][0]["font_type"] == expected
    assert result["project"]["track"][1][0]["bold"] is (role != "headline")


@pytest.mark.parametrize(
    "role,font_id",
    [
        ("body", "CenturyGothicBold"),
        ("headline", "CenturyGothic"),
        ("headline", "SY_Black"),
        ("headline", "SY_Black_JP"),
    ],
)
@pytest.mark.parametrize("use_url", [False, True])
def test_explicit_font_id_or_registered_url_wins(role, font_id, use_url, font_http):
    url = layout.FONT_RESOURCES[font_id]
    result = compile_one(
        visual_role=role, style={"font": url if use_url else font_id}
    )
    assert font_http == [url]
    assert result["layouts"]["line-1"]["measurement_font_type"] == url
    assert result["project"]["track"][1][0]["font_type"] == url
    assert result["project"]["track"][1][0]["bold"] is (role != "headline")


@pytest.mark.parametrize("role", ["headline", "body", "footnote", None, "legacy"])
@pytest.mark.parametrize(
    "manual_font", [None, "", "CenturyGothicBold", layout.CENTURY_GOTHIC_URL]
)
def test_missing_glyph_fallback_weight_preserves_manual_behavior(
    role, manual_font, font_http
):
    source = segment(
        visual_role=role,
        translated_text="Missing 漢字",
        style={"font": manual_font},
    )
    before = deepcopy(source)
    result = layout.compile_subtitle_project([source], video())
    assert source == before
    summary = result["layouts"]["line-1"]
    element = result["project"]["track"][1][0]
    expected = layout.FONT_RESOURCES.get(manual_font, manual_font) or (
        layout.CENTURY_GOTHIC_BOLD_URL
        if role == "headline"
        else layout.CENTURY_GOTHIC_URL
    )
    assert font_http == [expected]
    assert summary["measurement_font_type"] == expected
    assert summary["font_sha256"] is None
    assert summary["measurement_font_sha256"] is not None
    assert element["font_type"] == "ALi_PuHui"
    assert element["bold"] is (role != "headline" or not manual_font)
    assert element["text"] == source["translated_text"]
    payload = MediaKitMultiTrackClient._normalize_submit_payload(
        project=result["project"], idempotency_key="role-font-fallback"
    )
    assert payload["track"][1][0] == element
    assert summary["missing_glyphs"] == ["U+5B57", "U+6F22"]
    for warning in (
        "missing_glyphs",
        "font_fallback",
        "font_metrics_unverified",
        "preset_glyph_coverage_unverified",
    ):
        assert warning in summary["warnings"]
        assert f"line-1:{warning}" in result["warnings"]
    assert "real_render_unverified" in result["warnings"]


def test_role_font_is_selected_before_all_measurement(monkeypatch):
    # Distinct deterministic metrics expose measuring with the wrong role font.
    loaded = []

    class MetricsFont:
        def __init__(self, size, bold):
            self.size, self.bold = size, bold

        def getmetrics(self):
            return (self.size * (2 if self.bold else 1), 0)

        def getlength(self, text):
            return len(text) * self.size * (3 if self.bold else 1)

        def getbbox(self, text):
            return (0, 0, self.getlength(text), self.getmetrics()[0])

    class MetricsResource:
        def __init__(self, url):
            self.url = url
            self.sha256 = hashlib.sha256(url.encode()).hexdigest()
            self.codepoints = frozenset({ord("W")})

        def font(self, size):
            return MetricsFont(size, self.url == layout.CENTURY_GOTHIC_BOLD_URL)

    def load(url):
        loaded.append(url)
        return MetricsResource(url)

    monkeypatch.setattr(layout, "_load_font_resource", load)
    summaries = {}
    for role in ("headline", "body"):
        summaries[role] = compile_one(
            visual_role=role,
            bbox_normalized=[400, 200, 600, 300],
            translated_text="W" * 14,
            font_type=layout.CENTURY_GOTHIC_BOLD_URL,
        )["layouts"]["line-1"]
    assert loaded == [layout.CENTURY_GOTHIC_BOLD_URL, layout.CENTURY_GOTHIC_URL]
    headline, body = summaries["headline"], summaries["body"]
    assert headline["estimated_font_size_px"] == 36
    assert body["estimated_font_size_px"] == 72
    assert headline["desired_text_width_px"] == 1514
    assert body["desired_text_width_px"] == 1010
    assert headline["effective_font_size_px"] == 27
    assert body["effective_font_size_px"] == 72
    assert headline["measured_text_width_px"] == 1134
    assert body["measured_text_width_px"] == 1008
    assert headline["render_bbox_pixels"] != body["render_bbox_pixels"]
    assert headline["render_bbox_pixels"] == [66, 144, 1216, 216]
    assert body["render_bbox_pixels"] == [136, 144, 1146, 216]
    assert "bbox_expansion_limited" in headline["warnings"]
    assert "bbox_expansion_limited" not in body["warnings"]
    for summary in summaries.values():
        assert summary["original_bbox_pixels"] == [513, 144, 769, 216]
        assert "bbox_expanded" in summary["warnings"]


def test_role_font_layout_version():
    assert layout.LAYOUT_VERSION == "single-line-png-text-default-background-v8"
    assert layout.ENCODING_POLICY_VERSION == "configurable-crf-h264-v3"
    from video_subtitle_grounding import __version__

    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["version"] == __version__ == "0.3.8"


@pytest.mark.parametrize("role", ["headline", "body", "footnote"])
@pytest.mark.parametrize("background", [
    "#FFFFFF", "#F5F5F5", "#F4F5F5", "#000000", "#101010",
    "#111010", "#777777", "#0000FF", None, "invalid", "#FFFFFF99",
])
@pytest.mark.parametrize("panel", [None, "#33333359", "#FF0000FF", "#FFFFFF80", "#00000000", "ignored"])
def test_product_policy_preserves_colors_styles_and_layout(role, background, panel):
    source = segment(visual_role=role, background_color=background,
                     style={"background_color": panel, "font_size_px": 18,
                            "alignment": "right"})
    before = deepcopy(source)
    result = layout.compile_subtitle_project([source], video(video_category="product"))
    plain = layout.compile_subtitle_project(
        [{**source, "style": {**source["style"], "background_color": None}}],
        video(video_category="product"))
    assert source == before
    assert result == plain
    summary = result["layouts"]["line-1"]
    element = result["project"]["track"][1][0]
    expected = "#000000" if background in ("#FFFFFF", "#F5F5F5") else "#FFFFFF"
    assert summary["recommended_text_color"] == expected
    assert summary["text_color"] == element["font_color"] == expected + "FF"
    assert summary["background_color"] == "#00000000"
    assert "background_color" not in element
    assert element["font_size"] <= 18 and element["align_type"] == 2
    assert element["target_time"] == [100, 1000]
    assert not {"border_color", "border_width", "shadow"} & element.keys()
    assert "background_color" not in result["project"]["canvas"]


@pytest.mark.parametrize("background", ["#FFFFFF", "#000000", "#777777", None])
@pytest.mark.parametrize("panel", [None, "#FF0000FF", "#00000000", "#FFFFFF80"])
def test_explicit_unboxing_is_identical_to_legacy(background, panel):
    source = segment(background_color=background, style={"background_color": panel})
    assert layout.compile_subtitle_project([source], video(video_category="unboxing")) == (
        layout.compile_subtitle_project([source], video()))


def test_product_manual_text_color_and_contrast_use_actual_background():
    result = layout.compile_subtitle_project([segment(style={
        "text_color": "#FFFFFF", "background_color": "#000000FF",
        "font": "CenturyGothic", "font_size_px": 18,
    })], video(video_category="product"))
    summary = result["layouts"]["line-1"]
    assert summary["text_color"] == "#FFFFFFFF"
    assert summary["background_color"] == "#00000000"
    assert "low_contrast" in summary["warnings"]
    result = layout.compile_subtitle_project([segment(background_color=None, style={
        "background_color": "#FFFFFF",
    })], video(video_category="product"))
    assert "contrast_unverified" not in result["layouts"]["line-1"]["warnings"]
    assert "background_unknown" in result["layouts"]["line-1"]["warnings"]


@pytest.mark.parametrize("category", [None, "", "other", 1, [], {}])
def test_invalid_video_category_is_explicit(category):
    with pytest.raises(layout.SubtitleLayoutError, match="invalid_video_category"):
        layout.compile_subtitle_project([segment()], video(video_category=category))


def test_explicit_japanese_font_is_preserved():
    result = compile_one(style={"font": "SY_Black_JP"})
    assert (
        result["layouts"]["line-1"]["font_type"] == layout.FONT_RESOURCES["SY_Black_JP"]
    )


@pytest.mark.parametrize(
    "metadata",
    [
        {"width": 720, "height": 1280, "rotation": 90},
        {"width": 720, "height": 1280, "rotation": 270},
        {"encoded_width": 1280, "encoded_height": 720, "rotation": 90},
        {"encoded_width": 1280, "encoded_height": 720, "rotation": 270},
        {"display_width": 720, "display_height": 1280, "rotation": 90},
    ],
)
def test_rotation_uses_display_space_exactly_once(metadata):
    result = layout.compile_subtitle_project(
        [segment(bbox_normalized=[0, 0, 999, 999], translated_text="Hi")],
        video(**metadata),
    )
    assert result["project"]["canvas"]["width"] == 720
    assert result["project"]["canvas"]["height"] == 1280
    assert result["layouts"]["line-1"]["render_bbox_pixels"] == [0, 0, 720, 1280]
    assert "extra" not in result["project"]["track"][0][0]
    assert "source_rotation_real_render_unverified" in result["warnings"]


def test_bbox_maps_to_target_not_reference_or_preview_letterbox():
    source = segment(bbox_pixels=[192, 216, 960, 324], translated_text="Hi")
    result = layout.compile_subtitle_project([source], video(width=1280, height=720))
    assert result["layouts"]["line-1"]["render_bbox_pixels"] == [128, 144, 641, 216]
    assert result["project"]["track"][1][0]["extra"][0]["pos_x"] == 128


def test_time_clipping_and_exclusion_are_reported_in_original_order():
    segments = [
        segment(segment_id="tail", start_ms=9000, end_ms=12_000),
        segment(
            segment_id="after", start_ms=10_000, end_ms=11_000, translated_text=None
        ),
        segment(segment_id="before", start_ms=-100, end_ms=100),
        segment(segment_id="excluded", excluded=True, translated_text=None),
    ]
    result = layout.compile_subtitle_project(
        segments, video(reference_duration_ms=11_000)
    )
    summaries = list(result["layouts"].values())
    assert [s["segment_id"] for s in summaries] == [
        "tail",
        "after",
        "before",
        "excluded",
    ]
    assert summaries[0]["target_time"] == [9000, 10_000]
    assert summaries[2]["target_time"] == [0, 100]
    assert summaries[1]["warnings"] == ["time_outside_video"]
    assert summaries[3]["excluded"]
    assert "tail:time_clipped" in result["warnings"]
    assert "before:time_clipped" in result["warnings"]
    assert "video_duration_mismatch" in result["warnings"]


def test_empty_or_all_clipped_blocks_submission():
    for segments in (
        [],
        [segment(start_ms=10_000, end_ms=11_000)],
        [segment(excluded=True)],
    ):
        with pytest.raises(layout.SubtitleLayoutError, match="no_renderable_subtitles"):
            layout.compile_subtitle_project(segments, video())


def test_duration_warning_threshold():
    result = layout.compile_subtitle_project(
        [segment()], video(reference_duration_ms=10_250)
    )
    assert "video_duration_mismatch" not in result["warnings"]


@pytest.mark.parametrize("fps", [24, 25, 30, 50, 60])
def test_local_adapter_fps_contract_preserved(fps):
    result = layout.compile_subtitle_project([segment()], video(fps=fps))
    assert result["project"]["output"]["fps"] == fps
    assert ("fps_support_local_contract_only" in result["warnings"]) == (fps != 30)


@pytest.mark.parametrize("fps", [None, 29.97, 23.976, 120, float("nan")])
def test_unsupported_fps_fallback_explicit(fps):
    result = layout.compile_subtitle_project([segment()], video(fps=fps))
    assert result["project"]["output"]["fps"] == 30
    assert "fps_fallback_30" in result["warnings"]


@pytest.mark.parametrize("audio", [True, False])
def test_source_audio_is_preserved_without_duplicate_audio_track(audio):
    result = layout.compile_subtitle_project([segment()], video(has_audio=audio))
    project = result["project"]
    assert project["output"]["disable_audio"] is not audio
    assert project["track"][0][0]["source"] == video()["source_url"]
    assert all(e["type"] != "audio" for track in project["track"] for e in track)
    assert ("source_has_no_audio" in result["warnings"]) is not audio


def test_minimal_nonoverlapping_track_packing_and_boundary_touch():
    segments = [
        segment(segment_id="later", start_ms=1000, end_ms=2000),
        segment(segment_id="first", start_ms=0, end_ms=1000),
        segment(segment_id="overlay", start_ms=100, end_ms=900),
    ]
    result = layout.compile_subtitle_project(segments, video())
    assert len(result["project"]["track"]) == 3
    assert [e["target_time"] for e in result["project"]["track"][1]] == [
        [0, 1000],
        [1000, 2000],
    ]
    assert result["project"]["track"][2][0]["target_time"] == [100, 900]
    assert [s["track_index"] for s in result["layouts"].values()] == [1, 1, 2]


def test_no_editor_200_element_or_20_track_limits_and_no_truncation():
    segments = [
        segment(segment_id=f"s{i}", translated_text=f"Hi {i}") for i in range(201)
    ]
    result = layout.compile_subtitle_project(segments, video())
    assert len(result["layouts"]) == 201
    assert len(result["project"]["track"]) == 202
    assert sum(len(t) for t in result["project"]["track"]) == 202
    assert "provider_text_capacity_unverified" in result["warnings"]
    with pytest.raises(layout.SubtitleLayoutError, match="segment_capacity_exceeded"):
        layout.compile_subtitle_project(
            [segment(segment_id=f"s{i}") for i in range(501)], video()
        )


def test_literal_text_survives_json_without_ass_or_html_escaping():
    text = r'{\an8} <b>Über</b> \N & "quoted" 100% :'
    result = compile_one(translated_text=text)
    project = json.loads(json.dumps(result["project"]))
    element = project["track"][1][0]
    assert element["text"] == text
    assert "text_res" not in element
    assert "provider_literal_escaping_unverified" in result["warnings"]
    payload = MediaKitMultiTrackClient._normalize_submit_payload(
        project=project, idempotency_key="workspace:1:digest"
    )
    assert payload["track"][1][0]["text"] == text
    assert payload["canvas"] == project["canvas"]
    assert len(payload["client_token"]) <= 64


@pytest.mark.parametrize(
    "text,code",
    [
        (None, "missing_translation"),
        ("", "missing_translation"),
        (" \t", "missing_translation"),
        ("a\nb", "text_must_be_single_line"),
        ("a\u2028b", "text_must_be_single_line"),
        ("x" * 4001, "text_capacity_exceeded"),
    ],
)
def test_no_missing_translation_or_multiline_or_truncated_text(text, code):
    with pytest.raises(layout.SubtitleLayoutError, match=code):
        compile_one(translated_text=text)


def test_too_narrow_at_one_pixel_is_explicit_error_not_truncation():
    with pytest.raises(
        layout.SubtitleLayoutError, match="text_cannot_fit_at_minimum_font"
    ):
        compile_one(bbox_normalized=[100, 200, 101, 300], translated_text="W" * 4000)


def test_small_font_warns_and_positive_integer_limit():
    result = compile_one(style={"font_size_px": 5.8}, translated_text="Hi")
    summary = result["layouts"]["line-1"]
    assert summary["effective_font_size_px"] == 5
    assert "font_size_small" in summary["warnings"]
    assert "font_size_reduced" in summary["warnings"]


def test_subpixel_user_size_uses_provider_positive_integer_minimum_with_warning():
    result = compile_one(style={"font_size_px": 0.5}, translated_text="Hi")
    summary = result["layouts"]["line-1"]
    assert summary["effective_font_size_px"] == 1
    assert "font_size_minimum_applied" in summary["warnings"]


def test_capacity_notice_and_errors_distinguish_tool_limit_from_provider_guarantee():
    assert layout.CAPACITY_NOTICE in compile_one()["warnings"]
    with pytest.raises(layout.SubtitleLayoutError) as exc:
        layout.compile_subtitle_project([segment()] * 501, video())
    assert exc.value.code == "segment_capacity_exceeded"
    assert "not a provider capacity guarantee" in str(exc.value)


def test_inputs_are_not_mutated_and_warnings_have_no_signed_url():
    segments, metadata = [segment()], video()
    before = deepcopy((segments, metadata))
    result = layout.compile_subtitle_project(segments, metadata)
    assert (segments, metadata) == before
    assert "signature" not in json.dumps(result["warnings"])
    assert "Über" not in json.dumps(result["warnings"])
    assert "source_url" not in json.dumps(result["layouts"])


def test_font_url_allowlist_blocks_ssrf_before_http(font_http):
    with pytest.raises(layout.SubtitleLayoutError, match="font_resource_unavailable"):
        layout._load_font_resource("https://127.0.0.1/font.ttf")
    assert font_http == []


def test_unavailable_font_has_redacted_failure(monkeypatch):
    class FailedClient:
        def __enter__(self):
            raise httpx.ConnectError("secret query string and credentials")

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(layout.httpx, "Client", lambda **kwargs: FailedClient())
    with pytest.raises(layout.SubtitleLayoutError) as exc:
        compile_one()
    assert str(exc.value) == "font_resource_unavailable"


def test_font_cmap_format_12_skips_zero_glyph_and_supports_non_bmp():
    # Small sfnt cmap-only fixture tests the parser, not font measurement.
    header = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0)
    table = struct.pack(">4sIII", b"cmap", 0, 28, 40)
    cmap = struct.pack(">HHHHI", 0, 1, 3, 10, 12)
    subtable = struct.pack(">HHIII", 12, 0, 28, 0, 1)
    group = struct.pack(">III", 0x10000, 0x10002, 0)
    assert layout._font_codepoints(header + table + cmap + subtable + group) == {
        0x10001,
        0x10002,
    }


def test_reviewed_draft_summary_lookup():
    reviewed = {
        **segment(),
        "bbox_pixels": [192, 216, 960, 324],
        "style": {"font": "SY_Black", "font_size_px": 24},
    }
    metadata = {key: value for key, value in video().items() if key != "source_url"}
    result = layout.compile_subtitle_project(
        [reviewed],
        {**metadata, "url": video()["source_url"]},
    )
    summary = result["layouts"].get(reviewed["segment_id"], {})
    assert summary["effective_font_size_px"] <= 24
    assert summary["render_bbox_pixels"] == [128, 144, 641, 216]


@pytest.mark.parametrize(
    "metadata,code",
    [
        ({"width": 159}, "unsupported_canvas_dimensions"),
        ({"height": 8193}, "unsupported_canvas_dimensions"),
        ({"duration_ms": 0}, "invalid_video_duration"),
        ({"duration_ms": 10.5}, "invalid_video_duration"),
        ({"rotation": 45}, "unsupported_video_rotation"),
        ({"has_audio": None}, "video_audio_metadata_required"),
        ({"source_url": "http://example.com/video.mp4"}, "invalid_video_source"),
        (
            {"source_url": "https://user:password@example.com/video.mp4"},
            "invalid_video_source",
        ),
    ],
)
def test_explicit_media_boundaries(metadata, code):
    with pytest.raises(layout.SubtitleLayoutError, match=code):
        layout.compile_subtitle_project([segment()], video(**metadata))


def test_duplicate_segment_ids_do_not_overwrite_summary():
    with pytest.raises(layout.SubtitleLayoutError, match="invalid_segment_id"):
        layout.compile_subtitle_project([segment(), segment()], video())


@pytest.mark.parametrize(
    "bbox",
    [
        [0, 0, 1000, 999],
        [500, 0, 100, 999],
        [0, 10, 100, 10],
        [0, 0, float("nan"), 999],
    ],
)
def test_invalid_bbox_never_silently_clamps(bbox):
    with pytest.raises(layout.SubtitleLayoutError, match="invalid_bbox"):
        compile_one(bbox_normalized=bbox)


@pytest.mark.parametrize(
    "status,content",
    [
        (302, b""),
        (403, b""),
        (200, b"not a font"),
    ],
)
def test_font_redirect_or_error_or_bad_binary_is_not_system_fallback(
    monkeypatch, status, content
):
    original_client = HTTP_CLIENT
    monkeypatch.setattr(
        layout.httpx,
        "Client",
        lambda **kwargs: original_client(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status,
                    content=content,
                    headers={"Location": "https://127.0.0.1/font.ttf"},
                )
            ),
        ),
    )
    with pytest.raises(layout.SubtitleLayoutError, match="font_resource_unavailable"):
        compile_one()


def test_oversized_font_download_is_bounded(monkeypatch):
    monkeypatch.setattr(layout, "MAX_FONT_BYTES", 10)
    with pytest.raises(layout.SubtitleLayoutError, match="font_resource_too_large"):
        compile_one()


@pytest.mark.parametrize("alignment", ["left", "center", "right"])
def test_expands_before_shrinking_preserving_anchor_and_source(alignment):
    source = segment(
        bbox_normalized=[400, 200, 600, 300],
        alignment=alignment,
        translated_text="Long translated title",
    )
    before = deepcopy(source)
    summary = layout.compile_subtitle_project([source], video())["layouts"]["line-1"]
    original, box = summary["original_bbox_pixels"], summary["render_bbox_pixels"]
    assert source == before
    assert box[1::2] == original[1::2]
    assert box[2] - box[0] > original[2] - original[0]
    assert summary["effective_font_size_px"] == summary["estimated_font_size_px"]
    assert summary["measured_text_width_px"] <= box[2] - box[0]
    assert "bbox_expanded" in summary["warnings"]
    assert "expanded_background_unverified" in summary["warnings"]
    assert "font_size_reduced" not in summary["warnings"]
    if alignment == "center":
        assert box[0] + box[2] == original[0] + original[2]
    elif alignment == "left":
        assert box[0] == original[0]
    else:
        assert box[2] == original[2]


@pytest.mark.parametrize("alignment", ["left", "center", "right"])
def test_boundary_limit_then_shrink_and_no_regression(alignment):
    source = segment(alignment=alignment, translated_text="W" * 100)
    summary = layout.compile_subtitle_project([source], video())["layouts"]["line-1"]
    original, box = summary["original_bbox_pixels"], summary["render_bbox_pixels"]
    assert 64 <= box[0] <= original[0] < original[2] <= box[2] <= 1216
    assert summary["effective_font_size_px"] < summary["estimated_font_size_px"]
    assert "bbox_expansion_limited" in summary["warnings"]
    resource = layout._load_font_resource(summary["measurement_font_type"])
    previous = layout._largest_size(
        resource,
        summary["estimated_font_size_px"],
        lambda font: (
            layout._text_width(font, source["translated_text"])
            <= original[2] - original[0]
        ),
    )
    assert summary["effective_font_size_px"] >= previous


@pytest.mark.parametrize(
    "box,alignment",
    [
        ([0, 200, 200, 300], "left"),
        ([800, 200, 999, 300], "right"),
        ([0, 200, 200, 300], "center"),
    ],
)
def test_existing_edge_boxes_are_never_moved_or_clipped(box, alignment):
    result = compile_one(
        bbox_normalized=box, alignment=alignment, translated_text="W" * 100
    )
    summary = result["layouts"]["line-1"]
    original, rendered = summary["original_bbox_pixels"], summary["render_bbox_pixels"]
    assert 0 <= rendered[0] <= original[0] < original[2] <= rendered[2] <= 1280
    assert rendered[1::2] == original[1::2]


def test_neighbor_reservations_are_order_independent_and_do_not_collide():
    segments = [
        segment(
            segment_id="a",
            bbox_normalized=[100, 200, 300, 300],
            translated_text="W" * 50,
        ),
        segment(
            segment_id="b",
            bbox_normalized=[600, 200, 800, 300],
            translated_text="W" * 50,
        ),
    ]
    first = layout.compile_subtitle_project(segments, video())["layouts"]
    reverse = layout.compile_subtitle_project(list(reversed(segments)), video())[
        "layouts"
    ]
    for key in first:
        assert first[key]["render_bbox_pixels"] == reverse[key]["render_bbox_pixels"]
        assert (
            first[key]["effective_font_size_px"]
            == reverse[key]["effective_font_size_px"]
        )
    assert first["a"]["render_bbox_pixels"][2] <= first["b"]["render_bbox_pixels"][0]


@pytest.mark.parametrize(
    "neighbor_change",
    [
        {"start_ms": 1000, "end_ms": 2000},
        {"bbox_normalized": [600, 300, 800, 400]},
        {"excluded": True, "bbox_normalized": None},
        {"start_ms": 10000, "end_ms": 11000, "bbox_normalized": None},
    ],
)
def test_only_visible_temporal_and_vertical_neighbors_constrain(neighbor_change):
    source = segment(bbox_normalized=[100, 200, 300, 300], translated_text="W" * 50)
    neighbor = (
        segment(segment_id="b", bbox_normalized=[600, 200, 800, 300], **neighbor_change)
        if "bbox_normalized" not in neighbor_change
        else segment(segment_id="b", **neighbor_change)
    )
    alone = layout.compile_subtitle_project([source], video())["layouts"]["line-1"]
    together = layout.compile_subtitle_project([source, neighbor], video())["layouts"][
        "line-1"
    ]
    assert alone["render_bbox_pixels"] == together["render_bbox_pixels"]


def test_existing_overlap_keeps_boxes_and_warns():
    segments = [
        segment(segment_id="a", translated_text="W" * 50),
        segment(
            segment_id="b",
            bbox_normalized=[400, 200, 700, 300],
            translated_text="W" * 50,
        ),
    ]
    result = layout.compile_subtitle_project(segments, video())
    for summary in result["layouts"].values():
        assert summary["render_bbox_pixels"] == summary["original_bbox_pixels"]
        assert "original_bbox_overlap" in summary["warnings"]
        assert "bbox_expanded" not in summary["warnings"]


def test_short_text_does_not_expand():
    summary = compile_one(translated_text="Hi")["layouts"]["line-1"]
    assert summary["render_bbox_pixels"] == summary["original_bbox_pixels"]
    assert "bbox_expanded" not in summary["warnings"]
