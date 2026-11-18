"""Encoding target uses the video stream's bps, independent of audio/container."""
import pytest

from video_subtitle_grounding.metadata import parse_video_bit_rate, parse_video_display_metadata


@pytest.mark.parametrize("value,expected", [
    (7000, 7000), ("7000", 7000), ("7000000", 7000000),
    (True, None), (False, None), (None, None), ("N/A", None),
    (0, None), (-1, None), (7000.0, None), (float("inf"), None),
    ("7e3", None), ("7000.0", None), ("7000bps", None), (" 7000", None),
    ("", None), ("-7", None), ([], None), ({}, None),
])
def test_optional_bitrate_parser(value, expected):
    assert parse_video_bit_rate(value) == expected


@pytest.mark.parametrize("value", ["7000", "7000000", None, "N/A", "7000.1", True])
def test_probe_uses_video_stream_never_container_or_audio(value):
    metadata = parse_video_display_metadata({
        "streams": [
            {"codec_type": "audio", "bit_rate": "128000"},
            {"codec_type": "video", "codec_name": "h264", "width": 1280,
             "height": 720, "duration": "10", "bit_rate": value},
        ],
        "format": {"format_name": "mp4", "bit_rate": "9000000"},
    })
    assert metadata.video_bit_rate == parse_video_bit_rate(value)
    assert metadata.video_bit_rate_probed
