"""Extract dataclasses and helpers shared across modules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


CUSTOM_ENDPOINT_VENDOR = "customendpoint"


@dataclass(frozen=True)
class EndpointCall:
    """A single request this tool intends to make.

    This deliberately carries **no** credential information. API keys stay in
    the VS Code Secret Storage reference form inside chatLanguageModels.json and
    are never read, resolved, or logged by this tool.
    """

    url: str
    provider_name: str
    base_url: str

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EndpointCall(provider={self.provider_name!r}, url={self.url!r})"


@dataclass
class FetchResult:
    """Outcome of fetching one base_url belonging to one provider."""

    base_url: str
    provider_name: str
    success: bool
    model_ids: list[str] = field(default_factory=list)
    error: Optional[str] = None
    model_metadata: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class ProviderSyncResult:
    """Per-provider outcome, carrying enough detail for reporting."""

    name: str
    changed: bool = False
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    settings_keys_removed: list[str] = field(default_factory=list)
    kept: int = 0
    skipped_deletion: bool = False
    errors: list[str] = field(default_factory=list)
    # Entries that lacked a usable string id and were discarded from the
    # synced model list.
    discarded_invalid: list[Any] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class ModelSyncError(Exception):
    """Raised for unrecoverable input/configuration problems."""


def is_custom_endpoint(provider: Any) -> bool:
    """Return True when a provider entry is a user-defined OpenAI-compatible endpoint."""
    return isinstance(provider, dict) and provider.get("vendor") == CUSTOM_ENDPOINT_VENDOR


def normalize_id(raw: Any) -> str | None:
    """Return a stripped non-empty model id, else None."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def base_display_name(model_id: str) -> str:
    """Build a human-friendly display name from the last path segment of a model id.

    Only the final path segment is used so `deepseek-ai/deepseek-v4-flash`
    becomes "Deepseek V4 Flash" rather than "Deepseek Ai / Deepseek V4 Flash".
    """
    last_segment = model_id.rsplit("/", 1)[-1]
    for ch in ("-", "_", ".", ":"):
        last_segment = last_segment.replace(ch, " ")
    words = [w for w in last_segment.split() if w]
    titled = [_smart_title(w) for w in words]
    return " ".join(titled) or model_id


def _smart_title(word: str) -> str:
    """Title-case a name token while preserving deliberate casing.

    Rules (checked in order):
      * already mixed/upper case  -> keep verbatim   (Qwen3, 8B, GLM-5)
      * lowercase version token   -> upper-case      (v4 -> V4, k3 -> K3)
      * otherwise                 -> capitalise      (flash -> Flash)
    """
    if any(ch.isupper() for ch in word[1:]) or word.isupper():
        return word
    if re.fullmatch(r"[a-z]+\d[\w.]*|[a-z]\d+", word):
        return word.upper()
    return word[:1].upper() + word[1:]


def endpoint_base_urls(models: Any) -> list[str]:
    """Return de-duplicated, order-preserving base urls found in a models list."""
    seen: Dict[str, None] = {}
    if isinstance(models, list):
        for entry in models:
            if isinstance(entry, dict):
                url = entry.get("url")
                if isinstance(url, str) and url.strip():
                    seen.setdefault(url.strip(), None)
    return list(seen.keys())
