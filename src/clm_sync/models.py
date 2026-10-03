"""Extract dataclasses and helpers shared across modules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CUSTOM_ENDPOINT_VENDOR = "customendpoint"


@dataclass
class FetchResult:
    """Outcome of fetching one base_url belonging to one provider."""

    base_url: str
    provider_name: str
    success: bool
    model_ids: list[str] = field(default_factory=list)
    error: Optional[str] = None
    model_metadata: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Remote ids skipped as non-text (media) models during parsing
    # (video/image/audio/embedding/... matched against id/name). Carried so
    # the merge layer can report them and -- crucially -- skip deletion when
    # an endpoint yielded *only* such models (there is no text-model signal
    # to justify dropping local entries).
    filtered_non_text: list[str] = field(default_factory=list)


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
    # Remote model ids skipped because they look like non-text (media)
    # models (video/image/audio/embedding/... matched against id/name).
    # Display-only for the *addition* path: a filtered id is never added.
    # It does NOT authorise deletion either -- a locally stored model whose
    # id is still advertised (even if filtered) is kept, and an endpoint
    # that advertised only media models never triggers deletion of its own
    # url's models (see sync.merge_provider_models).
    filtered_non_text: list[str] = field(default_factory=list)
    # Endpoint urls that were fetched successfully but advertised no text
    # model at all (only media models, or nothing usable).  Their silence
    # carries no signal about which local models are stale, so models under
    # these urls are never deleted.  Display-only; does not affect ok.
    no_signal_endpoints: list[str] = field(default_factory=list)
    # Optional /v1/credits balance string, populated when credits are fetched.
    credits: Optional[str] = None
    # Index of this result's provider object inside the (post-sync) config
    # list.  Populated by sync_config; lets a consumer pair a result back to
    # its exact provider by index rather than relying on list-ordering.
    config_index: Optional[int] = None
    # Set when the provider declares no requestable endpoint url, so it could
    # not be synced at all.  Surfaced in the report instead of failing
    # silently.  Does not count against `ok` (it is a config issue, not a
    # request failure) and does not affect the delete guard.
    no_endpoints: bool = False
    # Cross-provider settings keys (any vendor) that referenced one of this
    # provider's removed model ids, e.g. "customendpoint/<name>/<id>", and
    # were pruned from other providers.  Populated by
    # sync.prune_cross_settings; display-only, does not affect ok or the
    # delete guard.
    cross_settings_removed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class ModelSyncError(Exception):
    """Raised for unrecoverable input/configuration problems."""


def is_custom_endpoint(provider: Any) -> bool:
    """Return True when a provider entry is a user-defined OpenAI-compatible endpoint."""
    return isinstance(provider, dict) and provider.get("vendor") == CUSTOM_ENDPOINT_VENDOR


def is_secret_placeholder(value: Any) -> bool:
    """Return True when *value* is a VS Code template reference such as
    ``${input:chat.lm.secret.<id>}``.

    Such a value is a reference, not a usable key.  When it cannot be resolved
    to a real key it must never be sent verbatim as an ``Authorization`` token;
    callers should treat it as "no key" instead.
    """
    return isinstance(value, str) and value.startswith("${input:")


def normalize_id(raw: Any) -> str | None:
    """Return a stripped non-empty model id, else None."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


# Substring markers: matched case-insensitively against a model's `id` and
# `name` (separators `-_ ` are also stripped before matching, so `re-rank`
# still hits `rerank`). These are long/distinctive enough for plain substring
# matching. NOTE: `vision` is deliberately NOT here -- it marks vision-capable
# *chat* models, which must keep syncing.
NON_TEXT_SUBSTRING_MARKERS = (
    "video",
    "audio",
    "rerank",  # rerank / reranker (plus re-rank via separator stripping)
    "whisper",
    "speech",
    "midjourney",
)

# Chinese equivalents, in case a display name carries them.  Kept as plain
# substrings (2-character roots are the natural unit in Chinese), but see
# NON_TEXT_CHINESE_EXEMPT below: a name that also carries an "understanding"
# word describes a vision/audio-capable *chat* model, not a media generator.
NON_TEXT_CHINESE_MARKERS = (
    "视频",
    "图像",
    "图片",
    "音频",
    "语音",
    "音乐",
)

# When a name carries one of these, the Chinese markers above are ignored:
# `图像理解` / `视频理解` / `图片理解` are VLM chat models (the Chinese
# equivalent of the deliberately-excluded `vision`), not generators.
NON_TEXT_CHINESE_EXEMPT = ("理解",)

# Short roots that collide with ordinary English words (`voice` in
# `invoice`, `dall` in `medallion`, `sora` in `sorami`, `imagen` in
# `imagenet`, `music` in `musical`, `image` in `imagery`, `embed` in
# `embedded`, `diffusion` in `diffusiongemma`).  These are matched only as
# standalone tokens, so the colliding words survive.  Derived forms that MUST
# still be caught are listed explicitly (`dalle` for `dalle3`,
# `embedding`/`embeddings` for `text-embedding-3-large`).
NON_TEXT_TOKEN_MARKERS = (
    "tts",
    "stt",
    "asr",  # automatic speech recognition
    "voice",
    "dall",
    "dalle",
    "sora",
    "imagen",
    "music",
    "image",
    "embed",
    "embedding",
    "embeddings",
    # `diffusion` names the *generation technique*, not the output modality:
    # `diffusiongemma-26b-a4b-it` is a discrete-diffusion *text* model (VLM
    # chat, image/video in -> text out) and must survive, while every real
    # image generator spells it as its own token (`stable-diffusion-xl`,
    # `text-to-image-diffusion`, `diffusion-3`).
    "diffusion",
)

_TOKEN_PATTERN_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _token_pattern(token: str) -> "re.Pattern[str]":
    """Return the cached standalone-token pattern for *token*.

    The boundary class is ``[a-z]`` (not ``[a-z0-9]``) so a digit suffix --
    the usual version spelling, e.g. ``tts1`` / ``sora2`` / ``imagen4`` --
    still matches, while a letter continuation (``matt``, ``stts``,
    ``asrock``, ``ttsx``, ``xtts``) does not.
    """
    pattern = _TOKEN_PATTERN_CACHE.get(token)
    if pattern is None:
        pattern = re.compile(r"(?<![a-z])" + re.escape(token) + r"(?![a-z])")
        _TOKEN_PATTERN_CACHE[token] = pattern
    return pattern


def is_non_text_model(model_id: Any, name: Any = None) -> bool:
    """Return True when a model looks like a non-text (media) model.

    Only the model's ``id`` and display ``name`` are inspected ("只看 id +
    name"). Matching is case-insensitive and runs in three passes per
    candidate string:

      1. long distinctive substrings (video/audio/rerank/whisper/...)
      2. standalone short tokens (tts/stt/asr/voice/dall/sora/imagen/music/
         image/embed/embedding/...), so `invoice` / `medallion` / `sorami` /
         `imagenet` / `musical` / `imagery` / `embedded` are not caught
      3. Chinese markers, skipped entirely when the string also carries an
         "understanding" word (`图像理解` is a VLM chat model)

    Non-string inputs never match.
    """
    candidates: List[Any] = [model_id, name]
    for text in candidates:
        if not isinstance(text, str) or not text:
            continue
        lowered = text.lower()
        compact = re.sub(r"[-_\s]+", "", lowered)
        for marker in NON_TEXT_SUBSTRING_MARKERS:
            if marker in lowered or marker in compact:
                return True
        for token in NON_TEXT_TOKEN_MARKERS:
            if _token_pattern(token).search(lowered):
                return True
        if not any(word in lowered for word in NON_TEXT_CHINESE_EXEMPT):
            for marker in NON_TEXT_CHINESE_MARKERS:
                if marker in lowered or marker in compact:
                    return True
    return False


def base_display_name(model_id: str) -> str:
    """Build a human-friendly display name from the last path segment of a model id.

    Only the final path segment is used so `deepseek-ai/deepseek-v4-flash`
    becomes "Deepseek V4 Flash" rather than "Deepseek Ai / Deepseek V4 Flash".
    """
    last_segment = model_id.rsplit("/", 1)[-1]
    # "." is not a separator so versions/dates keep their dots (2.0, v4.5, 2024.08.06).
    for ch in ("-", "_", ":"):
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
