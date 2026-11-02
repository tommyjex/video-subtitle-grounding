"""Compile and submit subtitle overlays; callers persist provider task IDs."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from time import monotonic

from .layout import compile_subtitle_project
from .logging import log_event
from .mediakit import MediaKitMultiTrackClient

logger = logging.getLogger(__name__)


class SubtitleRenderService:
    def __init__(self, settings=None, *, client=None, compiler=None, event_logger=None):
        self.client = client if client is not None else MediaKitMultiTrackClient(settings)
        self.compiler = compiler or compile_subtitle_project
        self.event_logger = event_logger or log_event

    async def submit(self, segments, video, *, idempotency_key):
        started = monotonic()
        compiled = await asyncio.to_thread(self.compiler, segments, video)
        task = await self.client.submit(
            project=compiled["project"], idempotency_key=idempotency_key,
        )
        self.event_logger(
            logger, "subtitle_translation.submitted", outcome="succeeded",
            provider_task_id=task.task_id, operation="render",
            segment_count=len(segments), duration_ms=round((monotonic() - started) * 1000),
        )
        # Do not return/persist the project containing the signed source URL.
        return {
            "task": asdict(task),
            "layouts": compiled["layouts"],
            "warnings": compiled["warnings"],
        }

    async def get_task(self, *, task_id):
        return asdict(await self.client.get_task(task_id=task_id))

    async def poll(self, *, task_id, **kwargs):
        completed = await self.client.poll(task_id=task_id, **kwargs)
        self.event_logger(
            logger, "subtitle_translation.polled", outcome="succeeded",
            provider_task_id=task_id, operation="render",
        )
        return asdict(completed)
