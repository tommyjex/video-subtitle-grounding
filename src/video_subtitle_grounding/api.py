"""Optional standalone HTTP API. Library imports do not require FastAPI."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import APIKeyHeader

from .config import Settings, get_settings
from .logging import JsonFormatter, log_event, tls_only_logger
from .media import RemoteVideoError
from .provider import VideoSubtitleGroundingProviderError
from .schemas import (
    VideoSubtitleGroundingError, VideoSubtitleGroundingErrorCode,
    VideoSubtitleGroundingRequest, VideoSubtitleGroundingResponse,
)
from .service import VideoSubtitleGroundingService
from .tls_logging import TlsLogHandler, TlsLogSink
from .validation import VideoSubtitleGroundingValidationError

_CLIENT_CODES = {
    VideoSubtitleGroundingErrorCode.INVALID_VIDEO_URL,
    VideoSubtitleGroundingErrorCode.INVALID_VIDEO_INPUT,
    VideoSubtitleGroundingErrorCode.MEDIA_PROBE_FAILED,
}
_api_key = APIKeyHeader(name="X-API-Key", auto_error=False)


def create_app(
    settings: Settings | None = None, *,
    service: VideoSubtitleGroundingService | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if settings.service_api_key is None:
        from .config import ConfigurationError
        raise ConfigurationError("SERVICE_API_KEY is required.")
    if service is None:
        settings.require_server_config()
    logger = logging.getLogger("video_subtitle_grounding")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sink = TlsLogSink.from_settings(settings)
        formatter = JsonFormatter()
        stdout_handler = logging.StreamHandler()
        stdout_handler.setFormatter(formatter)
        handlers = [stdout_handler]
        if sink is not None:
            handlers.append(TlsLogHandler(sink, formatter))
            sink.start()
        previous_level, previous_propagate = logger.level, logger.propagate
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in handlers:
            logger.addHandler(handler)
        owned_service = service is None
        active_service = None
        try:
            active_service = service or VideoSubtitleGroundingService(
                settings=settings, tls_only_logger=tls_only_logger(sink),
            )
            app.state.service = active_service
            app.state.slots = asyncio.Semaphore(settings.max_concurrent_extractions)
            log_event(logger, "service.lifecycle", outcome="started")
            yield
        finally:
            if owned_service and active_service is not None:
                client = active_service.provider.client
                await asyncio.to_thread(client.close)
            log_event(logger, "service.lifecycle", outcome="stopped")
            if sink is not None:
                await sink.aclose()
            for handler in handlers:
                logger.removeHandler(handler)
                handler.close()
            logger.setLevel(previous_level)
            logger.propagate = previous_propagate

    app = FastAPI(title="Video subtitle grounding", version="0.1.0", lifespan=lifespan)

    def authorize(key: str | None = Depends(_api_key)) -> None:
        expected = settings.service_api_key.get_secret_value()
        if key is None or not secrets.compare_digest(key.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Invalid API key.")

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.post(
        "/v1/subtitles/extract", response_model=VideoSubtitleGroundingResponse,
        dependencies=[Depends(authorize)],
    )
    async def extract(payload: VideoSubtitleGroundingRequest):
        slots = app.state.slots
        if slots.locked():
            raise HTTPException(status_code=429, detail="Extraction capacity reached.", headers={"Retry-After": "5"})
        async with slots:
            try:
                return await app.state.service.run(payload)
            except (RemoteVideoError, VideoSubtitleGroundingProviderError, VideoSubtitleGroundingValidationError) as exc:
                raise HTTPException(
                    status_code=422 if exc.code in _CLIENT_CODES else 502,
                    detail=VideoSubtitleGroundingError(
                        code=exc.code, phase=exc.phase, message=str(exc),
                    ).model_dump(mode="json"),
                ) from exc
    return app
