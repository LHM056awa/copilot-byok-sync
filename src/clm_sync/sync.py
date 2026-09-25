"""Core merge logic.

Guarantees implemented here:
  * immutable provider fields (name/vendor/apiKey/apiType) are never written
  * only vendor=customendpoint providers are ever mutated; entries of other
    vendors (even when they share a provider name) pass through untouched
  * existing model objects keep every hand-authored field
  * entries without a usable string id carry no meaning in the config file
    and are discarded from the synced model list in every mode
  * deletion (models + their top-level `settings` entries) only happens when
    every endpoint contributing to that provider succeeded
  * no write occurs when nothing actually changed
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import (
    FetchResult,
    ModelSyncError,
    ProviderSyncResult,
    base_display_name,
    endpoint_base_urls,
    is_custom_endpoint,
    normalize_id,
)

PROTECTED_PROVIDER_FIELDS = ("name", "vendor", "apiKey", "apiType")

MODEL_CONFIG_FIELDS = (
    "name",
    "url",
    "toolCalling",
    "vision",
    "maxInputTokens",
    "maxOutputTokens",
    "supportsReasoningEffort",
    "modelOptions",
)

DEFAULT_NEW_MODEL_FIELDS = {
    "toolCalling": True,
    "vision": True,
    "maxInputTokens": 1000000,
    "maxOutputTokens": 384000,
    "supportsReasoningEffort": ["max"],
}


@dataclass
class SyncOutcome:
    """Result of a whole-file synchronization pass."""

    config: List[Dict[str, Any]]
    changed: bool
    providers: List[ProviderSyncResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.providers)


def select_providers(
    config: Any, provider_names: Optional[Sequence[str]] = None
) -> List[Dict[str, Any]]:
    """Return vendor=customendpoint providers, optionally filtered by exact name.

    Raises ModelSyncError when a requested name is missing or when the same name
    is declared twice, so `providerNames` never silently syncs the wrong entry.
    """
    if not isinstance(config, list):
        raise ModelSyncError("chatLanguageModels.json must contain a JSON array")

    candidates = [p for p in config if is_custom_endpoint(p)]
    if not provider_names:
        return candidates

    wanted = list(provider_names)
    by_name: Dict[str, int] = {}
    for provider in candidates:
        name = provider.get("name")
        if isinstance(name, str):
            by_name[name] = by_name.get(name, 0) + 1

    duplicates = [n for n, count in by_name.items() if count > 1]
    if duplicates:
        raise ModelSyncError(
            "provider name(s) declared more than once among custom endpoints: "
            + ", ".join(repr(n) for n in sorted(duplicates))
            + "; duplicate names cannot be targeted reliably"
        )

    missing = [n for n in wanted if n not in by_name]
    if missing:
        available = ", ".join(sorted(by_name)) or "<none>"
        raise ModelSyncError(
            "unknown provider(s): " + ", ".join(repr(m) for m in missing)
            + f"; available custom endpoints: {available}"
        )

    wanted_set = set(wanted)
    # Match by identity: only customendpoint candidates whose name was requested
    # are returned. A non-customendpoint entry (e.g. the built-in "Copilot")
    # that merely shares a name is never included, so it can never be mutated.
    return [p for p in candidates if p.get("name") in wanted_set]


def plan_calls(providers: Iterable[Dict[str, Any]]) -> List[tuple]:
    """Build (provider, base_url) request pairs.

    Pairs carry the provider object itself (not just its name) so that
    providers that share a name can never mix up their results. Duplicate
    base urls within one provider are collapsed by ``endpoint_base_urls``.
    """
    pairs: List[tuple] = []
    for provider in providers:
        for base_url in endpoint_base_urls(provider.get("models")):
            pairs.append((provider, base_url))
    return pairs


def _index_existing(models: Any) -> Dict[str, Dict[str, Any]]:
    """Map usable model id -> its existing object, one entry per unique id."""
    index: Dict[str, Dict[str, Any]] = {}
    if isinstance(models, list):
        for entry in models:
            if not isinstance(entry, dict):
                continue
            model_id = normalize_id(entry.get("id"))
            if model_id is not None and model_id not in index:
                index[model_id] = entry
    return index


def merge_provider_models(
    provider_name: str,
    existing_models: Any,
    results: Sequence[FetchResult],
    *,
    allow_delete: bool,
) -> tuple[List[Dict[str, Any]], ProviderSyncResult]:
    """Merge fetched ids into existing models for one provider.

    Deletion requires both `allow_delete` and *every* relevant FetchResult to be
    successful; otherwise existing models are preserved untouched. Entries
    without a usable string id (or that are not objects at all) have no
    meaning in the config file, so they are discarded from the model list in
    every mode and reported via `ProviderSyncResult.discarded_invalid`.
    """
    result = ProviderSyncResult(name=provider_name)

    existing_index = _index_existing(existing_models)
    discarded: List[Any] = []
    if isinstance(existing_models, list):
        for entry in existing_models:
            if isinstance(entry, dict) and normalize_id(entry.get("id")) is not None:
                continue
            discarded.append(copy.deepcopy(entry))
    elif existing_models is not None:
        # A non-list 'models' value is structurally invalid and is replaced by
        # the merged list below; record it so the run report explains the loss.
        discarded.append(copy.deepcopy(existing_models))
    if discarded:
        result.discarded_invalid = discarded
    remote_by_url: Dict[str, List[str]] = {}
    remote_metadata: Dict[str, Dict[str, Any]] = {}
    for res in results:
        if res.success:
            remote_by_url.setdefault(res.base_url, []).extend(res.model_ids)
            for model_id, metadata in res.model_metadata.items():
                remote_metadata.setdefault(model_id, metadata)

    all_remote_ids: Dict[str, None] = {}
    for ids in remote_by_url.values():
        for model_id in ids:
            all_remote_ids.setdefault(model_id, None)

    all_succeeded = bool(results) and all(r.success for r in results)
    can_delete = allow_delete and all_succeeded

    merged: Dict[str, Dict[str, Any]] = {}

    # Keep existing models (with all their metadata) when still advertised, or
    # when deletion is not safe.
    for model_id, original in existing_index.items():
        if model_id in all_remote_ids or not can_delete:
            merged[model_id] = copy.deepcopy(original)

    # Add newly advertised models, preserving previous-url preference.
    for base_url, ids in remote_by_url.items():
        for model_id in ids:
            if model_id in merged:
                continue
            remote_model = remote_metadata.get(model_id, {})
            model = {
                key: copy.deepcopy(remote_model[key])
                for key in MODEL_CONFIG_FIELDS
                if key in remote_model
            }
            model["id"] = model_id
            model.setdefault("name", base_display_name(model_id))
            model.setdefault("url", base_url)
            for key, value in DEFAULT_NEW_MODEL_FIELDS.items():
                model.setdefault(key, copy.deepcopy(value))
            merged[model_id] = model

    if can_delete:
        result.removed = sorted(set(existing_index) - set(merged), key=str.lower)
    result.added = sorted(set(merged) - set(existing_index), key=str.lower)
    result.kept = len(set(existing_index) & set(merged))
    result.skipped_deletion = bool(results) and not can_delete

    ordered = [merged[k] for k in sorted(merged, key=str.lower)]
    return ordered, result


def apply_provider_sync(
    provider: Dict[str, Any], results: Sequence[FetchResult], *, allow_delete: bool
) -> ProviderSyncResult:
    """Merge models and prune orphaned `settings` entries in one provider (in place).

    Only model quotations that are actually removed get their settings entry
    deleted, which is the "sync delete must also drop reasoningEffort" rule.
    """
    name = provider.get("name") or "<unnamed>"
    fetched = [r for r in results if r.provider_name == name]

    new_models, result = merge_provider_models(
        name, provider.get("models"), fetched, allow_delete=allow_delete
    )

    for res in fetched:
        if not res.success:
            message = res.error or "request failed"
            result.errors.append(f"{res.base_url}: {message}")

    changed = False
    if provider.get("models") != new_models:
        provider["models"] = new_models
        changed = True

    removed_ids = set(result.removed)
    settings = provider.get("settings")
    if isinstance(settings, dict) and removed_ids:
        pruned = {
            key: value for key, value in settings.items() if key not in removed_ids
        }
        dropped = sorted(set(settings) - set(pruned), key=str.lower)
        if dropped:
            result.settings_keys_removed = dropped
            if pruned:
                provider["settings"] = pruned
            else:
                # Drop the field entirely rather than leaving an empty object.
                del provider["settings"]
            changed = True

    result.changed = changed
    return result


def _select_positions(
    config: List[Dict[str, Any]],
    provider_names: Optional[Sequence[str]],
) -> List[int]:
    """Return the config indices of the providers to sync.

    Selection goes through `select_providers` first so unknown/duplicate names
    keep raising `ModelSyncError`, then positions are re-derived restricted to
    vendor=customendpoint entries. A non-customendpoint entry that happens to
    share a name with a requested provider therefore never matches, which is
    what keeps built-in entries (e.g. the copilot provider) immutable.
    """
    selected = select_providers(config, provider_names)
    selected_ids = {id(p) for p in selected}
    if provider_names:
        return [
            i
            for i, p in enumerate(config)
            if is_custom_endpoint(p) and id(p) in selected_ids
        ]
    return [i for i, p in enumerate(config) if is_custom_endpoint(p)]


def sync_config(
    config: List[Dict[str, Any]],
    fetcher,
    provider_names: Optional[Sequence[str]] = None,
    *,
    allow_delete: bool = True,
) -> SyncOutcome:
    """Run the full sync pipeline over `config` without touching disk."""
    working = copy.deepcopy(config)
    providers = [working[i] for i in _select_positions(config, provider_names)]

    calls = plan_calls(providers)

    # Results are keyed by the provider object itself so providers that share
    # a name can never mix up their fetch results.
    results_by_provider: Dict[int, List[FetchResult]] = {}
    for provider, base_url in calls:
        name = provider.get("name") or "<unnamed>"
        results_by_provider.setdefault(id(provider), []).append(
            fetcher(name, base_url)
        )

    provider_results: List[ProviderSyncResult] = []
    for provider in providers:
        provider_results.append(
            apply_provider_sync(
                provider,
                results_by_provider.get(id(provider), []),
                allow_delete=allow_delete,
            )
        )

    changed = working != config
    return SyncOutcome(config=working, changed=changed, providers=provider_results)
