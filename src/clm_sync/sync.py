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
import inspect
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import (
    FetchResult,
    ModelSyncError,
    ProviderSyncResult,
    base_display_name,
    endpoint_base_urls,
    is_custom_endpoint,
    is_secret_placeholder,
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


def merge_provider_models(
    provider_name: str,
    existing_models: Any,
    results: Sequence[FetchResult],
    *,
    allow_delete: bool,
    sort_models: bool = False,
) -> tuple[List[Dict[str, Any]], ProviderSyncResult]:
    """Merge fetched ids into existing models for one provider.

    Deletion requires both `allow_delete` and *every* relevant FetchResult to be
    successful; otherwise existing models are preserved untouched. Entries
    without a usable string id (or that are not objects at all) have no
    meaning in the config file, so they are discarded from the model list in
    every mode and reported via `ProviderSyncResult.discarded_invalid`.

    Order semantics: existing entries keep their original relative order and
    duplicates of an id are all preserved; newly advertised models are
    appended at the end in the order the endpoints advertised them.  A run
    that changes nothing keeps the list byte-identical, so the file is not
    rewritten on no-op runs.  When *sort_models* is set, the merged list is
    instead written in ascending model-id order (case-sensitive
    lexicographic order).
    """
    result = ProviderSyncResult(name=provider_name)

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

    # Keep existing models (with all their metadata) when still advertised, or
    # when deletion is not safe.  Duplicates of an id are all kept or all
    # dropped together, so no locally authored entry is lost silently.
    ordered: List[Dict[str, Any]] = []
    surviving_ids: set = set()
    if isinstance(existing_models, list):
        for entry in existing_models:
            if not isinstance(entry, dict):
                continue
            model_id = normalize_id(entry.get("id"))
            if model_id is None:
                continue
            if model_id in all_remote_ids or not can_delete:
                ordered.append(copy.deepcopy(entry))
                surviving_ids.add(model_id)

    # Add newly advertised models in endpoint-advertised order, preserving
    # previous-url preference.
    for base_url, ids in remote_by_url.items():
        for model_id in ids:
            if model_id in surviving_ids:
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
            ordered.append(model)
            surviving_ids.add(model_id)

    existing_ids = set()
    if isinstance(existing_models, list):
        for entry in existing_models:
            if isinstance(entry, dict):
                entry_id = normalize_id(entry.get("id"))
                if entry_id is not None:
                    existing_ids.add(entry_id)

    if can_delete:
        result.removed = sorted(existing_ids - surviving_ids, key=str.lower)
    result.added = sorted(surviving_ids - existing_ids, key=str.lower)
    result.kept = len(existing_ids & surviving_ids)
    result.skipped_deletion = bool(results) and not can_delete

    # Optional canonical order: ascending model-id order in strict
    # lexicographic (dictionary) order, i.e. case-sensitive codepoint
    # comparison ("Zeta" sorts before "alpha").  A case-insensitive key
    # would create ties that a stable sort resolves by insertion order,
    # making the result non-deterministic; case-sensitive is both the
    # literal "字典序" and fully deterministic.
    if sort_models:
        ordered.sort(key=lambda entry: str(entry["id"]))
    return ordered, result


def apply_provider_sync(
    provider: Dict[str, Any],
    results: Sequence[FetchResult],
    *,
    allow_delete: bool,
    sort_models: bool = False,
) -> ProviderSyncResult:
    """Merge models and prune orphaned `settings` entries in one provider (in place).

    Only model quotations that are actually removed get their settings entry
    deleted, which is the "sync delete must also drop reasoningEffort" rule.

    Providers that declare no requestable endpoint (no `models`, or a `models`
    list with no urls) are not forced to acquire a `models: []` field: a
    provider with no requests returns an empty merged list, and writing that
    would fabricate a key the user never had.
    """
    name = provider.get("name") or "<unnamed>"
    fetched = [r for r in results if r.provider_name == name]

    new_models, result = merge_provider_models(
        name, provider.get("models"), fetched, allow_delete=allow_delete, sort_models=sort_models
    )

    for res in fetched:
        if not res.success:
            message = res.error or "request failed"
            result.errors.append(f"{res.base_url}: {message}")

    changed = False
    has_models_key = "models" in provider
    if (has_models_key or new_models) and provider.get("models") != new_models:
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


def _resolve_provider_key(
    provider: Dict[str, Any], key_resolver
) -> Optional[str]:
    """Return the provider's effective API key for outbound requests.

    The provider's immutable ``apiKey`` field is read (never written).  A
    plain literal value is used as-is.  A VS Code secret placeholder
    (``${input:...}``) is handed to *key_resolver*; when it cannot be
    resolved, the request is made **without** a key (a 401 will surface in
    the run report) rather than sending the placeholder text itself.  This
    keeps the failure mode safe: the placeholder is a reference, not a
    credential.
    """
    raw = provider.get("apiKey")
    if not isinstance(raw, str) or not raw:
        return None
    if key_resolver is None:
        if is_secret_placeholder(raw):
            # No resolver available: an unresolvable reference is not a key.
            return None
        return raw
    resolved = key_resolver(raw)
    if resolved is not None:
        return resolved
    # Resolution failed.  A literal value can still be tried as-is (it may be
    # a plain key the caller just didn't resolve); a placeholder must not be
    # sent verbatim as an Authorization token.
    return None if is_secret_placeholder(raw) else raw


def _fetcher_takes_key(fetcher) -> bool:
    """Return True when *fetcher* accepts an ``api_key`` argument.

    Positional-arity probing keeps the legacy two-argument
    ``fetcher(name, base_url)`` contract working unchanged, so existing
    callers (and the test suite) need no migration.  A fetcher declared
    with ``*args`` (VAR_POSITIONAL) can also take a third positional
    argument, so it is treated as key-accepting; without that check the key
    would be silently dropped and the request sent unauthenticated.
    """
    sig = inspect.signature(fetcher)
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()):
        return True
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    return len(positional) >= 3


def sync_config(
    config: List[Dict[str, Any]],
    fetcher,
    provider_names: Optional[Sequence[str]] = None,
    *,
    allow_delete: bool = True,
    key_resolver=None,
    sort_models: bool = False,
) -> SyncOutcome:
    """Run the full sync pipeline over `config` without touching disk.

    *fetcher* is called as ``fetcher(name, base_url, api_key)`` when it accepts
    three positional arguments (as the CLI does), or as the legacy
    ``fetcher(name, base_url)`` otherwise, so existing two-argument fetchers keep
    working.  *key_resolver* (optional) maps a provider's raw ``apiKey`` value to
    the actual key sent in the request, enabling VS Code secret placeholders to
    be resolved on the caller side without `sync` ever touching the encrypted
    store.  *sort_models* (default False) rewrites each provider's model list
    in ascending model-id order; when False the original order is preserved and
    new models are appended.
    """
    working = copy.deepcopy(config)
    providers = [working[i] for i in _select_positions(config, provider_names)]

    calls = plan_calls(providers)
    pass_key = _fetcher_takes_key(fetcher)

    # Results are keyed by the provider object itself so providers that share
    # a name can never mix up their fetch results.
    results_by_provider: Dict[int, List[FetchResult]] = {}
    for provider, base_url in calls:
        name = provider.get("name") or "<unnamed>"
        if pass_key:
            api_key = _resolve_provider_key(provider, key_resolver)
            results_by_provider.setdefault(id(provider), []).append(
                fetcher(name, base_url, api_key)
            )
        else:
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
                sort_models=sort_models,
            )
        )

    changed = working != config
    return SyncOutcome(config=working, changed=changed, providers=provider_results)
