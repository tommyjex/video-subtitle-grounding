"""Versioned, platform-independent video subtitle extraction."""

__version__ = "0.1.0"

from .config import Settings
from .schemas import VideoSubtitleGroundingRequest, VideoSubtitleGroundingResponse
from .service import VideoSubtitleGroundingService

__all__ = [
    "Settings",
    "VideoSubtitleGroundingRequest",
    "VideoSubtitleGroundingResponse",
    "VideoSubtitleGroundingService",
    "__version__",
]
