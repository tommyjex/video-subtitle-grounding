import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from video_subtitle_grounding.brand_rules import BRAND_RULE_VERSION, filter_brand_records
from video_subtitle_grounding.config import Settings
from video_subtitle_grounding.legacy import LegacyGroundingService, LegacyTranslationAdapter
from video_subtitle_grounding.provider import (
    LEGACY_VIDEO_SUBTITLE_GROUNDING_PROMPT, VIDEO_SUBTITLE_GROUNDING_PROMPT,
    ModelArkSubtitleGroundingSegment, ModelArkVideoSubtitleGroundingResult,
)
from video_subtitle_grounding.schemas import VideoSubtitleGroundingUsage
from video_subtitle_grounding.service import VideoSubtitleGroundingService
from video_subtitle_grounding.translation import ModelArkSubtitleTranslationAdapter
from test_service import prepared


def record(text, **changes):
    return dict(segment_id="s", text=text, block_id="brand", line_index=0,
                start_ms=100, end_ms=1000, **changes)


@pytest.mark.parametrize("text", [
    "Tineco", "  tInEcO  ", "Live Easy. Enjoy Life.", "LIVE EASY ENJOY LIFE",
    "  Live  Easy.\tEnjoy Life. ", "Ｌｉｖｅ Ｅａｓｙ． Ｅｎｊｏｙ Ｌｉｆｅ．",
])
def test_exact_brand_forms(text):
    item = record(text)
    result = filter_brand_records([item])
    assert result.kept == ()
    assert result.skipped_indexes == {0}
    assert item["text"] == text


@pytest.mark.parametrize("text", [
    "Tineco FLOOR ONE S7", "Powered by Tineco", "Live Easy", "Enjoy Life",
    "Live Easy. Enjoy Life. with FLOOR ONE", "Live Easv. Enjoy Life.",
    "Thank you for watching", "Tineco.", "T i n e c o",
])
def test_product_and_incomplete_phrases_remain(text):
    item = record(text)
    assert filter_brand_records([item]).kept == (item,)


def lines():
    return [dict(record("Live Easy."), segment_id="a", line_index=0),
            dict(record("Enjoy Life."), segment_id="b", line_index=1)]


def test_multiline_requires_common_time_block_and_consecutive_lines():
    original = lines()
    assert filter_brand_records(original).skipped_indexes == {0, 1}
    for changes in [
        {"block_id": "other"}, {"block_id": "legacy_1"}, {"block_id": None},
        {"line_index": 2}, {"line_index": None}, {"line_index": True},
        {"start_ms": 1000, "end_ms": 2000},
    ]:
        altered = [original[0], {**original[1], **changes}]
        assert filter_brand_records(altered).kept == tuple(altered)
    ambiguous = [*original, dict(original[1], segment_id="c")]
    assert filter_brand_records(ambiguous).kept == tuple(ambiguous)


def test_repeated_slogans_and_three_lines_require_common_intersection():
    repeated = [*lines(), *[{**r, "start_ms": 2000, "end_ms": 3000} for r in lines()]]
    assert filter_brand_records(repeated).skipped_indexes == {0, 1, 2, 3}
    three = [dict(record("Live"), line_index=0, end_ms=300),
             dict(record("Easy. Enjoy"), line_index=1, start_ms=200, end_ms=500),
             dict(record("Life."), line_index=2, start_ms=400, end_ms=700)]
    assert filter_brand_records(three).kept == tuple(three)
    three[0]["end_ms"] = 600
    assert filter_brand_records(three).skipped_indexes == {0, 1, 2}


@pytest.mark.parametrize("legacy", [False, True])
def test_extraction_filters_without_modifying_raw_response_or_logs(tmp_path, legacy):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"fixture")
    base = ModelArkSubtitleGroundingSegment(
        text="Tineco", start_ms=100, end_ms=1000, bbox="<bbox>100 100 900 200</bbox>")
    source = ModelArkVideoSubtitleGroundingResult(
        model="fixture", usage=VideoSubtitleGroundingUsage(), request_id=None,
        raw_output="PRIVATE_BRAND_RAW",
        subtitles=(base, replace(base, text="Live Easy.", block_id="outro", line_index=0),
                   replace(base, text="Enjoy Life.", block_id="outro", line_index=1),
                   replace(base, text="Tineco FLOOR ONE S7")),
    )

    class Provider:
        async def ground_subtitles(self, *args, **kwargs):
            return source

    events = []
    cls = LegacyGroundingService if legacy else VideoSubtitleGroundingService
    service = cls(settings=Settings(), provider=Provider(),
                  event_logger=lambda *args, **kw: events.append(kw))
    result = asyncio.run(service.extract(prepared(path)))
    assert [s.text for s in result.subtitles] == (
        [s.text for s in source.subtitles] if legacy else ["Tineco FLOOR ONE S7"])
    assert source.subtitles[0].text == "Tineco" and source.raw_output == "PRIVATE_BRAND_RAW"
    assert events[-1]["brand_filtered_count"] == (0 if legacy else 3)
    assert events[-1]["brand_rule_version"] == (None if legacy else BRAND_RULE_VERSION)
    assert "PRIVATE_BRAND_RAW" not in repr(events) and "Live Easy" not in repr(events)
    assert path.exists()


def test_prompt_and_legacy_strategy_are_distinct():
    assert "Live Easy. Enjoy Life." in VIDEO_SUBTITLE_GROUNDING_PROMPT
    assert "Live Easy. Enjoy Life." not in LEGACY_VIDEO_SUBTITLE_GROUNDING_PROMPT
    assert LegacyGroundingService.provider_class.extraction_prompt == LEGACY_VIDEO_SUBTITLE_GROUNDING_PROMPT


def test_translation_brand_only_avoids_translation_and_recognition_calls():
    create = Mock(side_effect=AssertionError("must not call model"))
    client = SimpleNamespace(responses=SimpleNamespace(
        with_raw_response=SimpleNamespace(create=create)))
    adapter = ModelArkSubtitleTranslationAdapter(Settings(), client=client)
    result = asyncio.run(adapter.translate(
        [record("Tineco")], segment_ids=["s"], source_language="auto", target_language="de"))
    assert result["translations"] == {} and result["failed_ids"] == []
    assert result["skipped_brand_ids"] == ["s"]
    assert not create.called
    assert LegacyTranslationAdapter.brand_rules_enabled is False


def test_translation_removes_brands_from_adjacent_context_and_product_recognition():
    payloads = []

    def create(**kwargs):
        data = json.loads(kwargs["input"][1]["content"][0]["text"])
        payloads.append(data)
        assert "Tineco" not in json.dumps(data)
        return {"output": [{"content": [{"type": "output_text", "text": json.dumps({
            "translations": [{"segment_id": "normal", "translated_text": "Produkt"}]
        })}]}]}

    client = SimpleNamespace(responses=SimpleNamespace(
        with_raw_response=SimpleNamespace(create=create)))
    adapter = ModelArkSubtitleTranslationAdapter(Settings(), client=client,
                                                 recognize_product_names=False)
    records = [record("Tineco"), dict(record("Useful feature"), segment_id="normal"),
               dict(record("Live Easy. Enjoy Life."), segment_id="slogan")]
    result = asyncio.run(adapter.translate(
        records, segment_ids=["normal", "slogan"], source_language="auto", target_language="de"))
    assert result["translations"] == {"normal": "Produkt"}
    assert result["failed_ids"] == [] and payloads[0]["context_only"] == []
    # Explicit name recognition also strips source brand records and its context.
    payloads.clear()
    asyncio.run(adapter.recognize_products(records, segment_ids=["normal"]))
    assert payloads and payloads[0]["context_only"] == []


def test_extraction_does_not_reconstruct_synthetic_fallback_visual_metadata(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"fixture")
    base = ModelArkSubtitleGroundingSegment(
        text="Live Easy.", start_ms=100, end_ms=1000,
        bbox="<bbox>010 100 900 200</bbox>", block_id=None, line_index=None)
    raw = ModelArkVideoSubtitleGroundingResult(
        model="fixture", usage=VideoSubtitleGroundingUsage(), request_id=None,
        raw_output="private", subtitles=(base, replace(
            base, text="Enjoy Life.", block_id="segment_0", line_index=1),
            replace(base, text="Tineco")),
    )

    class Provider:
        async def ground_subtitles(self, *args, **kwargs):
            return raw

    service = VideoSubtitleGroundingService(settings=Settings(), provider=Provider())
    response = asyncio.run(service.extract(prepared(path)))
    assert [s.text for s in response.subtitles] == ["Live Easy.", "Enjoy Life."]
