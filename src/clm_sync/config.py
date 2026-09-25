"""Load and atomically save chatLanguageModels.json.

Serialization round-trips through json.dumps so a successful parse guarantees a
serializable structure; `ensure_ascii=False` keeps non-ASCII model names intact.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, List, Union

from .models import ModelSyncError

PathLike = Union[str, "os.PathLike[str]"]
INDENT = "\t"


@dataclass(frozen=True)
class SaveOutcome:
    path: str
    written: bool
    reason: str = ""


def load_config(path: PathLike) -> List[dict[str, Any]]:
    """Read and validate the configuration file, returning provider objects."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data: Any = json.load(handle)
    except FileNotFoundError as exc:
        raise ModelSyncError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ModelSyncError(f"{path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ModelSyncError(f"cannot read {path}: {exc}") from exc

    _validate(data)
    return data


def _validate(data: Any) -> None:
    if not isinstance(data, list):
        raise ModelSyncError("chatLanguageModels.json must contain a JSON array")
    for index, provider in enumerate(data):
        if not isinstance(provider, dict):
            raise ModelSyncError(f"provider #{index} is not a JSON object")
        models = provider.get("models")
        if models is not None and not isinstance(models, list):
            raise ModelSyncError(
                f"provider {provider.get('name', index)!r} has a non-list 'models' field"
            )
        settings = provider.get("settings")
        if settings is not None and not isinstance(settings, dict):
            raise ModelSyncError(
                f"provider {provider.get('name', index)!r} has a non-object 'settings' field"
            )


def serialize(data: Any) -> str:
    text = json.dumps(data, indent=INDENT, ensure_ascii=False)
    return text + "\n"


def write_config_atomic(
    path: PathLike, data: Any, *, original_text: str | None = None
) -> SaveOutcome:
    """Write `data` to `path` via a same-directory temp file + atomic replace.

    Skips the write entirely when the rendered text is unchanged, which keeps
    mtime/OneDrive sync quiet on no-op runs.
    """
    text = serialize(data)

    if original_text is not None:
        _roundtrip_ok(text)
        if text == original_text:
            return SaveOutcome(str(path), written=False, reason="no changes detected")

    _roundtrip_ok(text)

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".clm-sync-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return SaveOutcome(str(path), written=True)


def _roundtrip_ok(text: str) -> None:
    """Re-parse rendered output before touching the user's file."""
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ModelSyncError(f"refusing to write invalid JSON: {exc}") from exc
