import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from video_subtitle_grounding.config import Settings
from video_subtitle_grounding.media import PreparedRemoteVideo
from video_subtitle_grounding.metadata import VideoDisplayMetadata
from video_subtitle_grounding.provider import (
    ModelArkSubtitleGroundingSegment, ModelArkVideoSubtitleGroundingResult,
)
from video_subtitle_grounding.schemas import (
    VideoSubtitleGroundingErrorCode, VideoSubtitleGroundingUsage,
)
from video_subtitle_grounding.service import VideoSubtitleGroundingService
from video_subtitle_grounding.validation import VideoSubtitleGroundingValidationError


def prepared(path: Path):
    return PreparedRemoteVideo(
        path=path, mime_type="video/mp4", size_bytes=100,
        metadata=VideoDisplayMetadata(
            encoded_width=1920, encoded_height=1080, display_width=1920,
            display_height=1080, duration_seconds=30, rotation=0,
            container="mp4", codec="h264",
        ),
    )


class Provider:
    def __init__(self, *, invalid=False, cancel=False):
        self.invalid, self.cancel = invalid, cancel

    async def ground_subtitles(self, path, *, sampling_fps):
        assert path.exists()
        assert sampling_fps == 2
        if self.cancel:
            raise asyncio.CancelledError
        return ModelArkVideoSubtitleGroundingResult(
            model="doubao-seed-evolving", usage=VideoSubtitleGroundingUsage(),
            subtitles=(ModelArkSubtitleGroundingSegment(
                text="Private model output", start_ms=1000, end_ms=2000,
                bbox="invalid" if self.invalid else "<bbox>100 100 900 200</bbox>",
            ),), raw_output="PRIVATE_RAW_RESPONSE", request_id=None,
        )


def test_borrowed_file_and_aggregate_logs(tmp_path):
    path = tmp_path / "input.mp4"
    path.write_bytes(b"video")
    events = []
    service = VideoSubtitleGroundingService(
        settings=Settings(), provider=Provider(),
        event_logger=lambda *args, **kwargs: events.append((args, kwargs)),
    )
    result = asyncio.run(service.extract(prepared(path)))
    assert path.exists()
    assert result.subtitles[0].bbox_normalized == (100, 100, 900, 200)
    assert result.video.width == 1920
    assert "Private model output" not in repr(events)
    assert "PRIVATE_RAW_RESPONSE" not in repr(events)
    assert events[-1][1]["subtitle_count"] == 1


@pytest.mark.parametrize("debug", [False, True])
def test_invalid_response_is_only_sent_to_explicit_tls_callback(tmp_path, debug):
    path = tmp_path / "input.mp4"
    path.write_bytes(b"video")
    events, raw = [], []
    service = VideoSubtitleGroundingService(
        settings=Settings(enable_experimental_video_subtitle_grounding_debug=debug),
        provider=Provider(invalid=True),
        event_logger=lambda *args, **kwargs: events.append((args, kwargs)),
        tls_only_logger=lambda *args, **kwargs: raw.append(kwargs),
    )
    with pytest.raises(VideoSubtitleGroundingValidationError) as exc:
        asyncio.run(service.extract(prepared(path)))
    assert exc.value.code == VideoSubtitleGroundingErrorCode.INVALID_MODEL_OUTPUT
    assert path.exists()
    assert "PRIVATE_RAW_RESPONSE" not in repr(events)
    assert len(raw) == int(debug)
    if debug:
        assert raw[0]["output_text"] == "PRIVATE_RAW_RESPONSE"


def test_owned_preparation_cleans_after_cancellation(tmp_path):
    path = tmp_path / "input.mp4"
    path.write_bytes(b"video")

    class Preparer:
        @asynccontextmanager
        async def open(self, url):
            try:
                yield prepared(path)
            finally:
                path.unlink()

    service = VideoSubtitleGroundingService(
        settings=Settings(), preparer=Preparer(), provider=Provider(cancel=True),
    )
    from video_subtitle_grounding.schemas import VideoSubtitleGroundingRequest
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run(VideoSubtitleGroundingRequest(video_url="https://example.test/input.mp4")))
    assert not path.exists()
