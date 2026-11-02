import asyncio
import io
import logging
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from video_subtitle_grounding.api import create_app
from video_subtitle_grounding.config import ConfigurationError, Settings
from video_subtitle_grounding.logging import JsonFormatter, log_event, tls_only_logger
from video_subtitle_grounding.media import RemoteVideoError
from video_subtitle_grounding.schemas import (
    VideoSubtitleGroundingErrorCode as Code, VideoSubtitleGroundingErrorPhase as Phase,
    VideoSubtitleGroundingResponse, VideoSubtitleGroundingUsage,
    VideoSubtitleGroundingVideoMetadata,
)


class Service:
    calls = 0
    failure = None

    async def run(self, request):
        self.calls += 1
        if self.failure:
            raise self.failure
        return VideoSubtitleGroundingResponse(
            model="fixture", subtitles=[], usage=VideoSubtitleGroundingUsage(),
            video=VideoSubtitleGroundingVideoMetadata(width=1280, height=720, duration_ms=3000),
            latency_ms=1,
        )


def test_health_auth_and_success():
    service = Service()
    app = create_app(Settings(service_api_key="test-api-key"), service=service)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        for headers in ({}, {"X-API-Key": "wrong-key"}):
            assert client.post("/v1/subtitles/extract", headers=headers, json={
                "video_url": "https://example.test/input.mp4",
            }).status_code == 401
        assert service.calls == 0
        response = client.post("/v1/subtitles/extract", headers={"X-API-Key": "test-api-key"}, json={
            "video_url": "https://example.test/input.mp4", "sampling_fps": 2,
        })
        assert response.status_code == 200
        assert response.json()["video"]["width"] == 1280
        assert service.calls == 1


@pytest.mark.parametrize("code,status", [(Code.INVALID_VIDEO_URL, 422), (Code.VIDEO_DOWNLOAD_FAILED, 502)])
def test_api_preserves_sanitized_failure_contract(code, status):
    service = Service()
    service.failure = RemoteVideoError(code=code, phase=Phase.DOWNLOAD, message="Video unavailable.")
    with TestClient(create_app(Settings(service_api_key="test-api-key"), service=service)) as client:
        response = client.post("/v1/subtitles/extract", headers={"X-API-Key": "test-api-key"}, json={
            "video_url": "https://example.test/input.mp4",
        })
    assert response.status_code == status
    assert response.json()["detail"] == {"code": code.value, "phase": "download", "message": "Video unavailable."}


def test_concurrency_limit_and_invalid_input_do_not_call_provider():
    service = Service()
    app = create_app(Settings(service_api_key="test-api-key"), service=service)
    with TestClient(app) as client:
        headers = {"X-API-Key": "test-api-key"}
        assert client.post("/v1/subtitles/extract", headers=headers, json={
            "video_url": "https://example.test/input.mp4", "sampling_fps": 0,
        }).status_code == 422
        client.portal.call(app.state.slots.acquire)
        try:
            response = client.post("/v1/subtitles/extract", headers=headers, json={
                "video_url": "https://example.test/input.mp4",
            })
            assert response.status_code == 429
            assert response.headers["Retry-After"] == "5"
        finally:
            client.portal.call(app.state.slots.release)
    assert service.calls == 0


def test_settings_never_read_platform_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ARK_API_KEY=must-not-load\nDB_NAME=production\n")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    settings = Settings.from_environment()
    assert settings.ark_api_key is None
    assert "db_name" not in Settings.model_fields
    with pytest.raises(ConfigurationError):
        create_app(settings)


def test_raw_debug_requires_tls_and_secrets_are_redacted():
    settings = Settings(ark_api_key="ark-secret", service_api_key="api-secret",
                        enable_experimental_video_subtitle_grounding_debug=True)
    with pytest.raises(ConfigurationError, match="requires TLS"):
        settings.require_server_config()
    assert "ark-secret" not in repr(settings)
    assert "api-secret" not in repr(settings)


def test_disabled_tls_allows_blank_optional_environment_secrets(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "ark-fixture")
    monkeypatch.setenv("SERVICE_API_KEY", "api-fixture")
    monkeypatch.setenv("TLS_ENABLED", "false")
    monkeypatch.setenv("TLS_ACCESS_KEY_ID", "")
    monkeypatch.setenv("TLS_SECRET_ACCESS_KEY", "")
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_VIDEO_SUBTITLE_GROUNDING_DEBUG", "false")
    settings = Settings.from_environment()
    settings.require_server_config()
    assert settings.tls_access_key_id is None
    assert settings.tls_secret_access_key is None


def test_model_text_cannot_enter_ordinary_json_logs():
    stream = io.StringIO()
    logger = logging.Logger("isolated")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    log_event(logger, "extract.completed", outcome="succeeded",
              subtitle_count=2, output_text="PRIVATE_RAW", video_url="SECRET_URL")
    assert "PRIVATE_RAW" not in stream.getvalue()
    assert "SECRET_URL" not in stream.getvalue()
    events = []
    emit = tls_only_logger(SimpleNamespace(emit=events.append))
    emit("debug", outcome="failed", output_text="PRIVATE_RAW")
    assert events[0]["output_text"] == "PRIVATE_RAW"


def test_library_does_not_import_platform_or_database():
    assert not any(name == "backend" or name.startswith("backend.") for name in sys.modules)
    assert "sqlalchemy" not in sys.modules
