"""Subtitle-specific output contract, independent of raster layout."""

DEFAULT_CRF = 16
ENCODING_POLICY_VERSION = "configurable-crf-h264-v3"


def validate_crf(value):
    if type(value) is not int or not 0 <= value <= 30:
        raise ValueError("invalid_crf")
    return value


def frozen_crf(snapshot):
    """Resolve legacy tasks using only their immutable configuration."""
    for value in (snapshot.get("crf"), (snapshot.get("encoding_result") or {}).get("crf")):
        try:
            return validate_crf(value)
        except ValueError:
            pass
    return DEFAULT_CRF


def apply_subtitle_output_policy(project, crf):
    crf = validate_crf(crf)
    project.setdefault("canvas", {}).pop("background_color", None)
    for track in project.get("track", []):
        for element in track:
            if element.get("type") == "text":
                element.pop("background_color", None)
    codec = project.setdefault("output", {}).setdefault("codec", {})
    codec.pop("video_bit_rate", None)
    codec.update(video_codec="h264", crf=crf)
    return project
