"""Compile and submit subtitle overlays; callers persist provider task IDs."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from time import monotonic

from .layout import (
    ENCODING_POLICY_VERSION, LAYOUT_VERSION, SubtitleLayoutError,
    compile_subtitle_project, compile_text_subtitle_project,
)
from .logging import log_event
from .encoding import DEFAULT_CRF, apply_subtitle_output_policy, validate_crf
from .mediakit import MediaKitMultiTrackClient

logger = logging.getLogger(__name__)


def _require_text_project(compiled, error_code):
    if compiled.get("layers") or any(
        element.get("type") != "text" or str(element.get("source", "")).startswith("subtitle-layer:")
        for track in compiled["project"].get("track", [])[1:] for element in track
    ):
        raise SubtitleLayoutError(error_code)


class SubtitleRenderService:
    def __init__(self, settings=None, *, client=None, compiler=None, event_logger=None,
                 layer_materializer=None):
        self.client = client if client is not None else MediaKitMultiTrackClient(settings)
        self.compiler = compiler or compile_subtitle_project
        self.fallback_compiler = compiler or compile_text_subtitle_project
        self.event_logger = event_logger or log_event
        self.layer_materializer = layer_materializer

    async def submit(self, segments, video, *, idempotency_key):
        started = monotonic()
        try:
            crf = validate_crf(video.get("crf", DEFAULT_CRF))
        except ValueError:
            raise SubtitleLayoutError("invalid_crf") from None
        render_mode = video.get("render_mode", "png")
        if render_mode not in ("png", "text"):
            raise SubtitleLayoutError("invalid_render_mode")
        video = {**video, "render_mode": render_mode}
        compiled = await asyncio.to_thread(self.compiler, segments, video)
        from .product_layout import materialize_layers

        if render_mode == "text":
            _require_text_project(compiled, "text_render_layers_unavailable")
        try:
            if compiled.get("layers"):
                await materialize_layers(compiled, self.layer_materializer)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - no unresolved source can reach the provider
            previous = compiled
            compiled = await asyncio.to_thread(
                self.fallback_compiler, segments, {**video, "render_mode": "text"})
            _require_text_project(compiled, "product_layer_materialization_unavailable")
            compiled.setdefault("warnings", []).extend(previous.get("warnings", []))
            for segment_id, summary in compiled.get("layouts", {}).items():
                summary.setdefault("warnings", []).extend(
                    previous.get("layouts", {}).get(segment_id, {}).get("warnings", []))
            compiled.setdefault("warnings", []).append("product_layer_materialization_unavailable")
            for summary in compiled.get("layouts", {}).values():
                summary.setdefault("warnings", []).append("product_layer_materialization_unavailable")
        apply_subtitle_output_policy(compiled["project"], crf)
        tracks = compiled["project"].get("track", [])
        elements = [element for track in tracks[1:] for element in track]
        types = {"png" if element["type"] == "image" else "text" for element in elements}
        modes = set()
        for summary in compiled["layouts"].values():
            if summary.get("excluded"):
                continue
            mode = summary.get("effective_render_mode")
            if mode is None:
                index = summary.get("track_index")
                row_types = {
                    "png" if element["type"] == "image" else "text"
                    for element in (tracks[index] if isinstance(index, int)
                                    and 0 < index < len(tracks) else elements)
                    if not summary.get("target_time")
                    or element.get("target_time") == summary["target_time"]
                }
                mode = next(iter(row_types)) if len(row_types) == 1 else render_mode
            summary.update(render_mode=render_mode, effective_render_mode=mode)
            modes.add(mode)
        actual_modes = types or modes
        effective_render_mode = next(iter(actual_modes)) if len(actual_modes) == 1 else (
            "mixed" if actual_modes else render_mode)
        task = await self.client.submit(
            project=compiled["project"], idempotency_key=idempotency_key,
        )
        self.event_logger(
            logger, "subtitle_translation.submitted", outcome="succeeded",
            provider_task_id=task.task_id, operation="render",
            segment_count=len(segments), duration_ms=round((monotonic() - started) * 1000),
            render_mode=render_mode, effective_render_mode=effective_render_mode,
            video_codec="h264", crf=crf, encoding_policy_version=ENCODING_POLICY_VERSION,
        )
        # Do not return/persist the project containing the signed source URL.
        return {
            "task": asdict(task),
            "layouts": compiled["layouts"],
            "warnings": compiled["warnings"],
            "render_mode": render_mode,
            "effective_render_mode": effective_render_mode,
            "layout_version": LAYOUT_VERSION,
            "encoding_policy_version": ENCODING_POLICY_VERSION,
            "video_codec": "h264",
            "crf": crf,
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
