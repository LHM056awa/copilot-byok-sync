"""Load and apply packaged model presets."""

from __future__ import annotations

import copy
import functools
import json
from importlib.resources import files
from typing import Any

from .models import DEFAULT_NEW_MODEL_FIELDS, ModelSyncError

ALLOWED_OVERRIDE_FIELDS = frozenset(
    {
        "toolCalling",
        "vision",
        "maxInputTokens",
        "maxOutputTokens",
        "supportsReasoningEffort",
        "modelOptions",
        "name",
    }
)
REMOTE_MODEL_FIELDS = ALLOWED_OVERRIDE_FIELDS


@functools.lru_cache(maxsize=1)
def _load_presets() -> tuple[dict[str, Any], ...]:
    # User override files are not supported in this release, so there is no
    # runtime invalidation path. Call _load_presets.cache_clear() if that
    # changes in a future release.
    try:
        text = files("clm_sync").joinpath("presets.json").read_text(encoding="utf-8")
        data: Any = json.loads(text)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise ModelSyncError(f"cannot load packaged model presets: {exc}") from exc

    if not isinstance(data, dict) or set(data) != {"presets"}:
        raise ModelSyncError("presets.json must contain only a 'presets' array")
    presets = data["presets"]
    if not isinstance(presets, list):
        raise ModelSyncError("presets.json 'presets' must be an array")

    validated: list[dict[str, Any]] = []
    for index, preset in enumerate(presets):
        if not isinstance(preset, dict) or set(preset) != {
            "model_id_contains",
            "overrides",
        }:
            raise ModelSyncError(
                f"preset #{index} must contain only model_id_contains and overrides"
            )
        markers = preset["model_id_contains"]
        if (
            not isinstance(markers, list)
            or not markers
            or any(not isinstance(marker, str) or not marker for marker in markers)
        ):
            raise ModelSyncError(
                f"preset #{index} model_id_contains must be a non-empty string array"
            )
        overrides = preset["overrides"]
        if not isinstance(overrides, dict):
            raise ModelSyncError(f"preset #{index} overrides must be an object")
        forbidden = set(overrides) - ALLOWED_OVERRIDE_FIELDS
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ModelSyncError(f"preset #{index} has forbidden or unknown fields: {names}")
        validated.append(
            {
                "model_id_contains": tuple(marker.casefold() for marker in markers),
                "overrides": copy.deepcopy(overrides),
            }
        )
    return tuple(validated)


def apply_presets(
    model_id: str, remote_metadata: dict[str, Any], is_new_model: bool
) -> dict[str, Any]:
    """Return a model's fields using remote metadata, preset, then defaults."""
    if not is_new_model:
        return copy.deepcopy(remote_metadata)

    preset_overrides: dict[str, Any] = {}
    model_id_folded = model_id.casefold()
    for preset in _load_presets():
        if any(marker in model_id_folded for marker in preset["model_id_contains"]):
            preset_overrides = preset["overrides"]
            break

    merged = copy.deepcopy(DEFAULT_NEW_MODEL_FIELDS)
    merged.update(copy.deepcopy(preset_overrides))
    merged.update(
        {
            key: copy.deepcopy(value)
            for key, value in remote_metadata.items()
            if key in REMOTE_MODEL_FIELDS
        }
    )
    return merged
