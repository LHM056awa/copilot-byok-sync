"""End-to-end tests using only fake transports - no network access."""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from clm_sync import cli
from clm_sync.client import TransportError, parse_models_payload, resolve_models_url
from clm_sync.config import load_config, serialize, write_config_atomic
from clm_sync.models import ModelSyncError, base_display_name
from clm_sync.sync import sync_config
from clm_sync.models import FetchResult


def _ok(url: str, timeout=None) -> str:
    raise AssertionError(f"unexpected real HTTP call to {url}")


def fake_transport(route: dict[str, object], api_key_sink: list = None):
    """Build a transport keyed by requested url.

    route values are either a body string or an Exception to raise.
    When *api_key_sink* is a list, the api_key passed per call is appended
    to it so tests can assert on it without any key material being printed.
    """

    def _call(url: str, timeout=None, api_key=None) -> str:
        if api_key_sink is not None:
            api_key_sink.append(api_key)
        entry = route.get(url)
        if entry is None:
            raise TransportError(f"HTTP 404 (no fixture for {url})")
        if isinstance(entry, Exception):
            raise entry
        return str(entry)

    return _call


def body(*ids: str) -> str:
    return json.dumps({"data": [{"id": i} for i in ids]})


def provider(
    name="TestProvider", models=None, settings=None, api_key="${input:secret}"
):
    entry = {
        "name": name,
        "vendor": "customendpoint",
        "apiKey": api_key,
        "apiType": "chat-completions",
        "models": models if models is not None else [],
    }
    if settings is not None:
        entry["settings"] = settings
    return entry


class ResolveUrlTest(unittest.TestCase):
    def test_plain_host_gets_v1_models(self):
        self.assertEqual(
            "https://api.example.com/v1/models",
            resolve_models_url("https://api.example.com"),
        )

    def test_trailing_slash(self):
        self.assertEqual(
            "https://api.example.com/v1/models",
            resolve_models_url("https://api.example.com/"),
        )

    def test_existing_v1_is_not_doubled(self):
        self.assertEqual(
            "https://api.example.com/v1/models",
            resolve_models_url("https://api.example.com/v1"),
        )
        self.assertEqual(
            "https://api.example.com/v1/models",
            resolve_models_url("https://api.example.com/v1/"),
        )

    def test_already_full_endpoint_is_idempotent(self):
        url = "https://api.example.com/v1/models"
        self.assertEqual(url, resolve_models_url(url))

    def test_prefixed_path(self):
        self.assertEqual(
            "https://api.example.com/base/v1/models",
            resolve_models_url("https://api.example.com/base/v1"),
        )

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            resolve_models_url("   ")

    def test_trailing_slash_on_chat_completions(self):
        self.assertEqual(
            "https://api.example.com/v1/models",
            resolve_models_url("https://api.example.com/v1/chat/completions/"),
        )

    def test_case_insensitive_chat_completions(self):
        self.assertEqual(
            "https://api.example.com/v1/models",
            resolve_models_url("https://api.example.com/V1/Chat/Completions/"),
        )

    def test_responses_subpath(self):
        self.assertEqual(
            "https://api.example.com/v1/models",
            resolve_models_url("https://api.example.com/v1/responses"),
        )

    def test_query_string_is_stripped(self):
        self.assertEqual(
            "https://api.example.com/v1/models",
            resolve_models_url("https://api.example.com/v1/models?limit=100"),
        )
        self.assertEqual(
            "https://api.example.com/v1/models",
            resolve_models_url("https://api.example.com/v1/chat/completions?foo=1"),
        )


class ParsePayloadTest(unittest.TestCase):
    def test_openai_shape(self):
        self.assertEqual(["a", "b"], parse_models_payload(body("a", "b")))

    def test_preserves_slash_id(self):
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "nvidia/nemotron-3-ultra-550b-a55b",
                        "name": "Nemotron 3 Ultra 550B",
                        "toolCalling": True,
                        "vision": True,
                        "maxInputTokens": 262144,
                        "maxOutputTokens": 65536,
                    }
                ]
            }
        )
        self.assertEqual(
            ["nvidia/nemotron-3-ultra-550b-a55b"], parse_models_payload(payload)
        )

    def test_deduplicates_preserving_order(self):
        self.assertEqual(["a", "b"], parse_models_payload(body("a", "b", "a")))

    def test_bare_list(self):
        payload = json.dumps([{"id": "x"}, {"id": "y"}])
        self.assertEqual(["x", "y"], parse_models_payload(payload))

    def test_skips_blank_and_non_string(self):
        payload = json.dumps({"data": [{"id": "x"}, {"id": "  "}, {"id": 5}, {}]})
        self.assertEqual(["x"], parse_models_payload(payload))

    def test_invalid_json_raises(self):
        with self.assertRaises(TransportError):
            parse_models_payload("not json")

    def test_empty_raises(self):
        with self.assertRaises(TransportError):
            parse_models_payload('{"data": []}')

    def test_single_object_data_is_wrapped(self):
        """A single model object (not a list) is accepted and treated as one entry."""
        payload = json.dumps({"data": {"id": "real-model", "object": "model", "name": "Real"}})
        self.assertEqual(["real-model"], parse_models_payload(payload))

    def test_string_data_is_rejected(self):
        """A bare string must NOT be iterated character-by-character into ids."""
        with self.assertRaises(TransportError):
            parse_models_payload('{"data": "gpt-4"}')

    def test_scalar_data_is_rejected(self):
        """A non-container scalar 'data' is a malformed payload, not an iterable of ids."""
        with self.assertRaises(TransportError):
            parse_models_payload('{"data": 12}')


class DisplayNameTest(unittest.TestCase):
    def test_strips_vendor_prefix(self):
        self.assertEqual(
            "Deepseek V4 Flash", base_display_name("deepseek-ai/deepseek-v4-flash")
        )

    def test_version_tokens_uppercased(self):
        self.assertEqual("Qwen3 8B", base_display_name("Qwen3-8B"))


class SyncTest(unittest.TestCase):
    def test_new_model_keeps_remote_metadata(self):
        model_id = "nvidia/nemotron-3-ultra-550b-a55b"
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": model_id,
                        "object": "model",
                        "created": 735790403,
                        "owned_by": "nvidia",
                        "name": "Nemotron 3 Ultra 550B",
                        "toolCalling": True,
                        "vision": True,
                        "maxInputTokens": 262144,
                        "maxOutputTokens": 65536,
                    }
                ]
            }
        )
        cfg = [provider(models=[{"id": "old", "name": "Old", "url": "https://x.test"}])]
        outcome = sync_config(
            cfg,
            lambda n, u: _fetch(n, u, {"https://x.test/v1/models": payload}),
        )
        added = next(
            model for model in outcome.config[0]["models"] if model["id"] == model_id
        )
        self.assertEqual("Nemotron 3 Ultra 550B", added["name"])
        self.assertTrue(added["toolCalling"])
        self.assertEqual(262144, added["maxInputTokens"])
        self.assertNotIn("object", added)
        self.assertNotIn("created", added)
        self.assertNotIn("owned_by", added)

    def test_adds_new_models_with_minimal_object(self):
        cfg = [
            provider(
                models=[
                    {
                        "id": "old",
                        "name": "Old",
                        "url": "https://x.test",
                        "vision": True,
                    }
                ]
            )
        ]
        route = {"https://x.test/v1/models": body("old", "new-model")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))

        models = outcome.config[0]["models"]
        # Default: existing entries keep their position; new models are appended.
        self.assertEqual(["old", "new-model"], [m["id"] for m in models])
        new_model = next(m for m in models if m["id"] == "new-model")
        self.assertEqual(
            {
                "id",
                "name",
                "url",
                "toolCalling",
                "vision",
                "maxInputTokens",
                "maxOutputTokens",
                "supportsReasoningEffort",
            },
            set(new_model),
        )
        self.assertEqual("https://x.test", new_model["url"])
        self.assertTrue(new_model["toolCalling"])
        self.assertTrue(new_model["vision"])
        self.assertEqual(1000000, new_model["maxInputTokens"])
        self.assertEqual(384000, new_model["maxOutputTokens"])
        self.assertEqual(["max"], new_model["supportsReasoningEffort"])

    def test_preserves_existing_metadata(self):
        original = {
            "id": "old",
            "name": "Custom Name",
            "url": "https://x.test",
            "toolCalling": True,
            "vision": False,
            "maxInputTokens": 1000,
            "maxOutputTokens": 2000,
            "supportsReasoningEffort": ["max"],
            "modelOptions": {"top_p": 0.95},
        }
        cfg = [provider(models=[copy.deepcopy(original)])]
        route = {"https://x.test/v1/models": body("old")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))
        self.assertEqual(original, outcome.config[0]["models"][0])

    def test_deletes_model_and_its_settings_entry(self):
        cfg = [
            provider(
                models=[
                    {"id": "kept", "name": "Kept", "url": "https://x.test"},
                    {"id": "gone", "name": "Gone", "url": "https://x.test"},
                ],
                settings={
                    "kept": {"reasoningEffort": "high"},
                    "gone": {"reasoningEffort": "max"},
                },
            )
        ]
        route = {"https://x.test/v1/models": body("kept")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))

        self.assertEqual(["kept"], [m["id"] for m in outcome.config[0]["models"]])
        self.assertEqual(
            {"kept": {"reasoningEffort": "high"}}, outcome.config[0]["settings"]
        )
        self.assertEqual(["gone"], outcome.providers[0].removed)
        self.assertEqual(["gone"], outcome.providers[0].settings_keys_removed)

    def test_removes_emptied_settings_field(self):
        cfg = [
            provider(
                models=[{"id": "gone", "name": "Gone", "url": "https://x.test"}],
                settings={"gone": {"reasoningEffort": "max"}},
            )
        ]
        route = {"https://x.test/v1/models": body("other")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))
        self.assertNotIn("settings", outcome.config[0])

    def test_no_delete_preserves_models_and_settings(self):
        cfg = [
            provider(
                models=[{"id": "gone", "name": "Gone", "url": "https://x.test"}],
                settings={"gone": {"reasoningEffort": "max"}},
            )
        ]
        route = {"https://x.test/v1/models": body("other")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route), allow_delete=False)
        self.assertEqual(
            ["gone", "other"], [m["id"] for m in outcome.config[0]["models"]]
        )
        self.assertEqual(
            {"gone": {"reasoningEffort": "max"}}, outcome.config[0]["settings"]
        )

    def test_failed_endpoint_preserves_everything(self):
        cfg = [
            provider(
                models=[{"id": "gone", "name": "Gone", "url": "https://x.test"}],
                settings={"gone": {"reasoningEffort": "max"}},
            )
        ]
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, {}))
        self.assertFalse(outcome.changed)
        self.assertEqual(["gone"], [m["id"] for m in outcome.config[0]["models"]])
        self.assertEqual(
            {"gone": {"reasoningEffort": "max"}}, outcome.config[0]["settings"]
        )
        self.assertTrue(outcome.providers[0].errors)

    def test_partial_multi_url_failure_skips_deletion(self):
        cfg = [
            provider(
                models=[
                    {"id": "a", "name": "A", "url": "https://one.test"},
                    {"id": "b", "name": "B", "url": "https://two.test"},
                ]
            )
        ]
        route = {"https://one.test/v1/models": body("a")}  # two.test missing -> fails
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))
        ids = [m["id"] for m in outcome.config[0]["models"]]
        self.assertEqual(["a", "b"], ids, "no model may be dropped when any url fails")

    def test_multi_url_success_unions_and_can_delete(self):
        cfg = [
            provider(
                models=[
                    {"id": "a", "name": "A", "url": "https://one.test"},
                    {"id": "b", "name": "B", "url": "https://two.test"},
                    {"id": "stale", "name": "S", "url": "https://one.test"},
                ]
            )
        ]
        route = {
            "https://one.test/v1/models": body("a"),
            "https://two.test/v1/models": body("b"),
        }
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))
        self.assertEqual(["a", "b"], [m["id"] for m in outcome.config[0]["models"]])
        self.assertEqual(["stale"], outcome.providers[0].removed)

    def test_provider_selection_by_name(self):
        cfg = [
            provider(
                name="A", models=[{"id": "a1", "name": "A1", "url": "https://a.test"}]
            ),
            provider(
                name="B", models=[{"id": "b1", "name": "B1", "url": "https://b.test"}]
            ),
            {"name": "Copilot", "vendor": "copilot"},
        ]
        route = {"https://a.test/v1/models": body("a1")}
        outcome = sync_config(
            cfg, lambda n, u: _fetch(n, u, route), provider_names=["A"]
        )
        self.assertEqual(1, len(outcome.providers))
        self.assertEqual("A", outcome.providers[0].name)
        # untouched B preserved verbatim
        self.assertEqual(cfg[1], outcome.config[1])

    def test_copilot_is_never_targeted(self):
        cfg = [
            {
                "name": "Copilot",
                "vendor": "copilot",
                "settings": {"auto": {"tier": "x"}},
            }
        ]
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, {}))
        self.assertEqual([], outcome.providers)
        self.assertEqual(cfg, outcome.config)

    def test_builtin_entry_sharing_name_is_untouched(self):
        """Regression: a non-customendpoint entry that shares its name with a
        requested provider must never be read, merged, or mutated."""
        builtin = {
            "name": "Copilot",
            "vendor": "copilot",
            "models": [{"id": "x", "name": "X", "url": "https://a.test"}],
        }
        cfg = [
            copy.deepcopy(builtin),
            provider(
                name="Copilot",
                models=[{"id": "c", "name": "C", "url": "https://a.test"}],
            ),
        ]
        route = {"https://a.test/v1/models": body("c", "new")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))
        # The built-in entry is byte-for-byte identical to the input.
        self.assertEqual(builtin, outcome.config[0])
        # Only the customendpoint entry was targeted.
        self.assertEqual(1, len(outcome.providers))
        self.assertEqual(["c", "new"], [m["id"] for m in outcome.config[1]["models"]])

    def test_named_selection_ignores_non_custom_endpoint_same_name(self):
        builtin = {
            "name": "Copilot",
            "vendor": "copilot",
            "models": [{"id": "x", "name": "X", "url": "https://a.test"}],
        }
        cfg = [
            copy.deepcopy(builtin),
            provider(
                name="Copilot",
                models=[{"id": "c", "name": "C", "url": "https://a.test"}],
            ),
        ]
        route = {"https://a.test/v1/models": body("c")}
        outcome = sync_config(
            cfg,
            lambda n, u: _fetch(n, u, route),
            provider_names=["Copilot"],
        )
        self.assertEqual(builtin, outcome.config[0])
        self.assertEqual(["c"], [m["id"] for m in outcome.config[1]["models"]])

    def test_entries_without_usable_id_are_discarded(self):
        """Regression: entries without a usable string id have no meaning in the
        config file, so they are discarded from the synced model list in every
        mode (no-delete and delete), and reported via discarded_invalid."""
        seed = [
            {"name": "NoId", "url": "https://x.test"},
            {"id": 7, "name": "NumId", "url": "https://x.test"},
            "stray-entry",
            {"id": "ok", "name": "Ok", "url": "https://x.test"},
        ]
        cfg = [provider(models=copy.deepcopy(seed))]
        route = {"https://x.test/v1/models": body("ok")}
        for allow_delete in (False, True):
            outcome = sync_config(
                cfg,
                lambda n, u: _fetch(n, u, route),
                allow_delete=allow_delete,
            )
            # Only the entry with a valid string id survives.
            self.assertEqual(["ok"], [m["id"] for m in outcome.config[0]["models"]])
            # The three invalid entries are reported, not silently dropped.
            self.assertEqual(
                [
                    {"name": "NoId", "url": "https://x.test"},
                    {"id": 7, "name": "NumId", "url": "https://x.test"},
                    "stray-entry",
                ],
                outcome.providers[0].discarded_invalid,
            )
            # Discarding invalid entries is not counted as a model removal.
            self.assertEqual([], outcome.providers[0].removed)

    def test_unknown_provider_name_errors(self):
        cfg = [provider(name="A")]
        with self.assertRaises(ModelSyncError):
            sync_config(cfg, lambda n, u: _fetch(n, u, {}), provider_names=["Nope"])

    def test_duplicate_provider_name_errors(self):
        cfg = [
            provider(
                name="Dup", models=[{"id": "x", "name": "X", "url": "https://d.test"}]
            ),
            provider(
                name="Dup", models=[{"id": "y", "name": "Y", "url": "https://d.test"}]
            ),
        ]
        with self.assertRaises(ModelSyncError):
            sync_config(cfg, lambda n, u: _fetch(n, u, {}), provider_names=["Dup"])

    def test_immutable_provider_fields_untouched(self):
        """sync_config must not rewrite name/vendor/apiKey/apiType on the
        output provider.  (Regression: an earlier version of this test compared
        the input against a freshly-built provider, so it passed vacuously and
        protected nothing.)"""
        api_key = "${input:test-secret}"
        cfg = [
            provider(
                models=[{"id": "seed", "name": "S", "url": "https://x.test"}],
                api_key=api_key,
            )
        ]
        route = {"https://x.test/v1/models": body("seed", "brand-new")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))

        synced = outcome.config[0]
        for field, expected in (
            ("name", "TestProvider"),
            ("vendor", "customendpoint"),
            ("apiKey", api_key),
            ("apiType", "chat-completions"),
        ):
            self.assertEqual(expected, synced[field])

    def test_same_url_different_providers_are_independent(self):
        """Shared base_url must not let one provider's result serve another."""
        url = "https://shared.test"
        cfg = [
            provider(name="P1", models=[{"id": "seed", "name": "S", "url": url}]),
            provider(name="P2", models=[{"id": "seed", "name": "S", "url": url}]),
        ]
        calls = []

        def fetcher(name, base_url):
            calls.append((name, base_url))
            return _fetch(name, base_url, {f"{url}/v1/models": body("seed", name)})

        outcome = sync_config(cfg, fetcher)
        self.assertEqual([("P1", url), ("P2", url)], calls)
        self.assertIn("P1", [m["id"] for m in outcome.config[0]["models"]])
        self.assertIn("P2", [m["id"] for m in outcome.config[1]["models"]])

    def test_noop_run_reports_unchanged(self):
        models = [
            {"id": "a", "name": "A", "url": "https://x.test"},
            {"id": "b", "name": "B", "url": "https://x.test"},
        ]
        cfg = [provider(models=copy.deepcopy(models))]
        route = {"https://x.test/v1/models": body("a", "b")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))
        self.assertFalse(outcome.changed)

    def test_models_append_new_and_preserve_order_by_default(self):
        """Default semantics: existing entries keep their hand-authored order
        and new models are appended at the end (no re-sorting).  A run with
        no substantive change is a no-op."""
        cfg = [
            provider(
                models=[
                    {"id": "zeta", "name": "Z", "url": "https://x.test"},
                    {"id": "alpha", "name": "A", "url": "https://x.test"},
                ]
            )
        ]
        route = {"https://x.test/v1/models": body("zeta", "alpha")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))
        self.assertEqual(["zeta", "alpha"], [m["id"] for m in outcome.config[0]["models"]])
        self.assertFalse(outcome.changed, "no substantive change -> no rewrite")

    def test_sort_models_orders_by_id_ascending(self):
        """With sort_models=True the merged list is written in ascending model-id
        order in strict lexicographic (dictionary) order: case-sensitive codepoint
        comparison, so uppercase ids sort before lowercase ones ('Zeta' < 'alpha')."""
        cfg = [
            provider(
                models=[
                    {"id": "zeta", "name": "Z", "url": "https://x.test"},
                    {"id": "alpha", "name": "A", "url": "https://x.test"},
                    {"id": "Zeta", "name": "Zeta", "url": "https://x.test"},
                ]
            )
        ]
        route = {"https://x.test/v1/models": body("alpha", "zeta", "Zeta")}
        outcome = sync_config(
            cfg, lambda n, u: _fetch(n, u, route), sort_models=True
        )
        # Case-sensitive lexicographic: 'Z' (0x5A) < 'a' (0x61).
        self.assertEqual(
            ["Zeta", "alpha", "zeta"], [m["id"] for m in outcome.config[0]["models"]]
        )
        self.assertTrue(outcome.changed, "first sorted sync of an unsorted list rewrites")

        # Second run over the already-sorted list is a no-op.
        outcome2 = sync_config(
            outcome.config, lambda n, u: _fetch(n, u, route), sort_models=True
        )
        self.assertFalse(outcome2.changed)

    def test_sort_models_orders_new_models_into_place(self):
        cfg = [
            provider(models=[{"id": "Zeta", "name": "Z", "url": "https://x.test"}])
        ]
        route = {"https://x.test/v1/models": body("Zeta", "alpha")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route), sort_models=True)
        ids = [m["id"] for m in outcome.config[0]["models"]]
        self.assertEqual(["Zeta", "alpha"], ids)
        self.assertEqual(["alpha"], outcome.providers[0].added)

    def test_duplicate_local_ids_all_preserved_no_delete(self):
        """--no-delete must not silently drop locally-duplicated ids; all copies
        are kept (the docs promise 'never remove existing models')."""
        existing = [
            {"id": "dup", "name": "First", "url": "https://x.test", "keep": 1},
            {"id": "dup", "name": "Second", "url": "https://x.test", "keep": 2},
            {"id": "ok", "name": "Ok", "url": "https://x.test"},
        ]
        cfg = [provider(models=copy.deepcopy(existing))]
        route = {"https://x.test/v1/models": body("dup", "ok")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route), allow_delete=False)
        ids = [m["id"] for m in outcome.config[0]["models"]]
        self.assertEqual(["dup", "dup", "ok"], ids)
        keeps = [m.get("keep") for m in outcome.config[0]["models"]]
        self.assertEqual([1, 2, None], keeps)

    def test_duplicate_local_ids_all_removed_when_stale(self):
        """When deletion is allowed and the id is no longer advertised, every
        duplicate copy is dropped together (reported once)."""
        existing = [
            {"id": "dup", "name": "First", "url": "https://x.test"},
            {"id": "dup", "name": "Second", "url": "https://x.test"},
            {"id": "ok", "name": "Ok", "url": "https://x.test"},
        ]
        cfg = [provider(models=copy.deepcopy(existing))]
        route = {"https://x.test/v1/models": body("ok")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route), allow_delete=True)
        ids = [m["id"] for m in outcome.config[0]["models"]]
        self.assertEqual(["ok"], ids)
        self.assertIn("dup", outcome.providers[0].removed)

    def test_provider_without_models_key_is_not_given_empty_list(self):
        """A customendpoint that declares no requestable endpoint must not be
        forced to acquire a `models: []` field - it has no urls, so no request
        is made and the file must stay unchanged."""
        empty = {"name": "Empty", "vendor": "customendpoint", "apiType": "chat-completions"}
        outcome = sync_config([empty], lambda n, u: FetchResult(u, n, True, ["x"]))
        self.assertFalse(outcome.changed)
        self.assertNotIn("models", outcome.config[0])

    def test_three_arg_fetcher_receives_resolved_key(self):
        """A fetcher accepting three positional args must be called with the
        key_resolver's output, so the real key reaches the transport without
        ever being printed here."""
        cfg = [
            provider(
                name="P",
                models=[{"id": "seed", "name": "S", "url": "https://x.test"}],
                api_key="${input:chat.lm.secret.-x}",
            )
        ]
        seen_keys: list = []

        def fetcher(name, base_url, api_key):
            seen_keys.append(api_key)
            return _fetch(name, base_url, {"https://x.test/v1/models": body("seed")})

        def resolver(raw):
            # stand-in for secrets.resolve_placeholder
            return "resolved-key-value"

        outcome = sync_config(cfg, fetcher, key_resolver=resolver)
        self.assertEqual(["resolved-key-value"], seen_keys)
        self.assertFalse(outcome.providers[0].errors)

    def test_literal_api_key_passes_through_without_resolver(self):
        cfg = [
            provider(
                name="P",
                models=[{"id": "seed", "name": "S", "url": "https://x.test"}],
                api_key="sk-literal",
            )
        ]
        seen_keys: list = []

        def fetcher(name, base_url, api_key):
            seen_keys.append(api_key)
            return _fetch(name, base_url, {"https://x.test/v1/models": body("seed")})

        sync_config(cfg, fetcher)  # no key_resolver -> literal used as-is
        self.assertEqual(["sk-literal"], seen_keys)

    def test_legacy_two_arg_fetcher_is_still_supported(self):
        """Backward compatibility: a fetcher with two positional args must keep
        working without receiving the key argument."""
        cfg = [
            provider(
                name="P",
                models=[{"id": "seed", "name": "S", "url": "https://x.test"}],
                api_key="${input:chat.lm.secret.-x}",
            )
        ]
        seen: list = []

        def fetcher(name, base_url):
            seen.append((name, base_url))
            return _fetch(name, base_url, {"https://x.test/v1/models": body("seed")})

        outcome = sync_config(cfg, fetcher, key_resolver=lambda raw: "unused")
        self.assertEqual([("P", "https://x.test")], seen)
        self.assertFalse(outcome.providers[0].errors)

    def test_unresolvable_placeholder_is_not_sent_verbatim(self):
        """A ${input:...} reference that fails to resolve must degrade to
        'no key' — the placeholder text itself must never ride the wire as
        an Authorization token."""
        cfg = [
            provider(
                name="P",
                models=[{"id": "seed", "name": "S", "url": "https://x.test"}],
                api_key="${input:chat.lm.secret.-gone}",
            )
        ]
        seen_keys: list = []

        def fetcher(name, base_url, api_key):
            seen_keys.append(api_key)
            return _fetch(name, base_url, {"https://x.test/v1/models": body("seed")})

        # Case 1: resolver present but returns None (lookup missed).
        sync_config(cfg, fetcher, key_resolver=lambda raw: None)
        self.assertIsNone(seen_keys[0], "unresolved placeholder must be keyless")

        # Case 2: no resolver at all — still keyless, never the template text.
        seen_keys.clear()
        sync_config(cfg, fetcher)
        self.assertIsNone(seen_keys[0], "placeholder without resolver must be keyless")

    def test_varargs_fetcher_receives_key(self):
        """Regression: a fetcher declared as ``def fetch(*args)`` accepts a
        third positional argument, so the resolved key must be passed through
        rather than being silently dropped (which would send the request
        unauthenticated)."""
        cfg = [
            provider(
                name="P",
                models=[{"id": "seed", "name": "S", "url": "https://x.test"}],
                api_key="${input:chat.lm.secret.-x}",
            )
        ]
        seen_keys: list = []

        def fetcher(*args):
            # args == (name, base_url, api_key)
            seen_keys.append(args[2] if len(args) > 2 else None)
            return _fetch(args[0], args[1], {"https://x.test/v1/models": body("seed")})

        sync_config(cfg, fetcher, key_resolver=lambda raw: "the-key")
        self.assertEqual(["the-key"], seen_keys)

    def test_read_secret_copies_wal_sidecars(self):
        """Regression: when a running VS Code instance leaves WAL sidecars next
        to state.vscdb, _read_secret must copy them alongside the main file so
        the snapshot is consistent; otherwise un-checkpointed pages are lost
        and the lookup degrades to 'no key' (a 401)."""
        import os
        import sqlite3
        import tempfile
        from unittest.mock import patch as _patch

        from clm_sync import secrets

        copied: list = []

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "state.vscdb")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE ItemTable (key TEXT, value BLOB)")
            con.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("secret://chat.lm.secret.-x", b'{"type":"Buffer","data":[]}'),
            )
            con.commit()
            con.close()
            # Simulate a running instance that left WAL sidecars behind.
            with open(db + "-wal", "wb") as fh:
                fh.write(b"WAL-snapshot")
            with open(db + "-shm", "wb") as fh:
                fh.write(b"SHM-snapshot")

            # Point the real _read_secret at this fixture and watch the copy
            # destination via a wrapper around shutil.copyfile.
            import shutil as _shutil

            orig_copy = _shutil.copyfile

            def spy_copy(src, dst, *a, **kw):
                copied.append(dst)
                return orig_copy(src, dst, *a, **kw)

            with _patch.object(secrets, "_global_storage_db_path", return_value=db), _patch.object(
                secrets, "_load_master_key", return_value=b"0" * 32
            ), _patch.object(_shutil, "copyfile", side_effect=spy_copy):
                # A 32-byte master key lets _read_secret reach the copy step;
                # the empty data payload then decrypts to None (no crash).
                secrets._read_secret("secret://chat.lm.secret.-x")

            main_copies = [c for c in copied if c.endswith("state.vscdb")]
            self.assertEqual(1, len(main_copies), "main file must be copied")
            # The sidecars are copied next to the main copy, in order.
            self.assertTrue(
                any(c.endswith("state.vscdb-wal") for c in copied),
                "WAL sidecar must be copied alongside the main file",
            )
            self.assertTrue(
                any(c.endswith("state.vscdb-shm") for c in copied),
                "SHM sidecar must be copied alongside the main file",
            )


class ConfigIoTest(unittest.TestCase):
    def test_roundtrip_preserves_structure(self):
        original = [provider(models=[{"id": "a", "name": "A", "url": "u"}])]
        self.assertEqual(original, json.loads(serialize(original)))

    def test_atomic_write_skipped_when_identical(self):
        data = [provider(models=[{"id": "a", "name": "A", "url": "u"}])]
        text = serialize(data)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cfg.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            outcome = write_config_atomic(path, json.loads(text), original_text=text)
            self.assertFalse(outcome.written)

    def test_atomic_write_replaces_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cfg.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(serialize([{"name": "old"}]))
            new = [provider(models=[{"id": "a", "name": "A", "url": "u"}])]
            outcome = write_config_atomic(path, new)
            self.assertTrue(outcome.written)
            self.assertEqual(new, load_config(path))
            self.assertEqual([], [f for f in os.listdir(tmp) if f.endswith(".tmp")])

    def test_load_rejects_non_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cfg.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"not": "an array"}')
            with self.assertRaises(ModelSyncError):
                load_config(path)


class SecretsTest(unittest.TestCase):
    """Placeholder resolution using fully synthetic v10 fixtures.

    No VS Code state database is ever touched; the expected payload is built
    here with a known key, so key material in this module is fixture-only.
    """

    KEY = "0123456789abcdef0123456789abcdef"  # exactly 32 bytes (256 bits)
    PLAINTEXT = "sk-synthetic-test-key"

    def test_decrypt_v10_roundtrip(self):
        from clm_sync.secrets import _decrypt_v10
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = self.KEY.encode()
        nonce = b"\x00" * 12
        payload = b"v10" + nonce + AESGCM(key).encrypt(nonce, self.PLAINTEXT.encode(), None)
        self.assertEqual(self.PLAINTEXT, _decrypt_v10(payload, key))

    def test_read_secret_via_injected_database(self):
        import json as _json
        import os
        import sqlite3
        import tempfile
        from unittest.mock import patch as _patch

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        from clm_sync import secrets

        nonce = b"\x07" * 12
        ct_and_tag = AESGCM(self.KEY.encode()).encrypt(nonce, self.PLAINTEXT.encode(), None)
        envelope = _json.dumps({"type": "Buffer", "data": list(b"v10" + nonce + ct_and_tag)})

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "state.vscdb")
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE ItemTable (key TEXT, value BLOB)")
            con.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("secret://chat.lm.secret.-test", envelope.encode()),
            )
            con.commit()
            con.close()

            # patch.object restores the originals on exit; a bare
            # assignment + del would permanently clobber the real module
            # functions and poison every later test in the module.
            with _patch.object(secrets, "_global_storage_db_path", return_value=db_path), _patch.object(
                secrets, "_load_master_key", return_value=self.KEY.encode()
            ):
                resolved = secrets.resolve_placeholder("${input:chat.lm.secret.-test}")
        # Only the fixture constant is compared; never any real key.
        self.assertEqual(self.PLAINTEXT, resolved)

    def test_secret_resolution_failures_return_none(self):
        """No resolution failure (missing package, damaged Local State, locked
        DB) may escape resolve_placeholder - they all degrade to None."""
        import os
        import sqlite3
        import tempfile
        from unittest.mock import patch as _patch

        from clm_sync import secrets

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "state.vscdb")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE ItemTable (key TEXT, value BLOB)")
            con.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("secret://chat.lm.secret.-x", b'{"type":"Buffer","data":[]}'),
            )
            con.commit()
            con.close()
            bad_ls = os.path.join(tmp, "Local State")
            with open(bad_ls, "w", encoding="utf-8") as fh:
                fh.write("{not json")

            with _patch.object(secrets, "_global_storage_db_path", return_value=db), _patch.object(
                secrets, "_load_master_key", return_value=b"0" * 32
            ):
                # Empty data -> no payload -> None
                self.assertIsNone(
                    secrets.resolve_placeholder("${input:chat.lm.secret.-x}")
                )
                # Missing cryptography must surface as None, never an exception
                def boom(payload, aes_key):
                    raise secrets.SecretResolutionError(
                        "cryptography package is required"
                    )

                with _patch.object(secrets, "_decrypt_v10", side_effect=boom):
                    self.assertIsNone(
                        secrets.resolve_placeholder("${input:chat.lm.secret.-x}")
                    )

            # A damaged Local State must yield a None master key, not a crash.
            # Only the path is redirected; the real _load_master_key runs
            # against the malformed file.  Assert on `is None` only so the key
            # material (if any) never appears in a failure message.
            with _patch.object(secrets, "_local_state_path", return_value=bad_ls):
                self.assertIsNone(secrets._load_master_key())

    def test_is_secret_placeholder(self):
        from clm_sync.models import is_secret_placeholder

        self.assertTrue(is_secret_placeholder("${input:chat.lm.secret.-abc}"))
        self.assertTrue(is_secret_placeholder("${input:anything-else}"))
        self.assertFalse(is_secret_placeholder("sk-literal"))
        self.assertFalse(is_secret_placeholder(None))


class CliTest(unittest.TestCase):
    def test_package_main_entry_executes_cli(self):
        """Regression: `python -m clm_sync` must actually run the CLI."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "clm_sync", "--version"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode)
        self.assertIn("clm-sync", result.stdout)

    def test_dry_run_does_not_write(self):
        cfg = [
            provider(
                models=[{"id": "seed", "name": "S", "url": "https://x.test"}],
                settings={"seed": {"reasoningEffort": "max"}},
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cfg.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(serialize(cfg))
            before = open(path, encoding="utf-8").read()
            with patch(
                "clm_sync.cli.fetch_models",
                return_value=FetchResult(
                    base_url="https://x.test",
                    provider_name="TestProvider",
                    success=True,
                    model_ids=["seed"],
                ),
            ):
                code = cli.run(["--config", path, "--all", "--dry-run"])
            self.assertEqual(cli.EXIT_OK, code)
            self.assertEqual(before, open(path, encoding="utf-8").read())

    def test_missing_config_returns_config_error(self):
        self.assertEqual(
            cli.EXIT_CONFIG_ERROR, cli.run(["--config", "nope-missing.json", "--all"])
        )

    def test_failed_endpoint_without_changes_reports_errors_to_stderr(self):
        """Regression: a 401/network failure that keeps the file unchanged must
        still surface the per-provider errors (to stderr), not just exit code 1."""
        cfg = [provider(models=[{"id": "seed", "name": "S", "url": "https://x.test"}])]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cfg.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(serialize(cfg))
            before = open(path, encoding="utf-8").read()

            def failing(name, base_url, timeout=None, api_key=None):
                return FetchResult(
                    base_url=base_url,
                    provider_name=name,
                    success=False,
                    error="HTTP 401",
                )

            stderr = io.StringIO()
            with (
                patch("clm_sync.cli.fetch_models", side_effect=failing),
                patch("sys.stderr", stderr),
            ):
                code = cli.run(["--config", path, "--all"])
            self.assertEqual(cli.EXIT_PARTIAL_FAILURE, code)
            self.assertEqual(before, open(path, encoding="utf-8").read())
            self.assertIn("HTTP 401", stderr.getvalue())

    def test_config_fixture_parses(self):
        data = [
            provider(name="TestProvider"),
            {"name": "Copilot", "vendor": "copilot", "models": []},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "chatLanguageModels.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(serialize(data))
            data = load_config(path)
        names = [p.get("name") for p in data if p.get("vendor") == "customendpoint"]
        self.assertIn("TestProvider", names)
        self.assertIn("Copilot", [p.get("name") for p in data])


def _fetch(name, base_url, route):
    from clm_sync.client import fetch_models

    return fetch_models(name, base_url, timeout=1.0, transport=fake_transport(route))


if __name__ == "__main__":
    unittest.main()
