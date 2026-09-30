"""End-to-end tests using only fake transports - no network access."""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from clm_sync import cli
from clm_sync.client import (
    TransportError,
    _CREDITS_ENDPOINTS,
    _json_path,
    fetch_credits,
    has_credits_endpoint,
    parse_credits_payload,
    parse_models_payload,
    resolve_models_url,
)
from clm_sync.config import load_config, serialize, write_config_atomic
from clm_sync.models import ModelSyncError, base_display_name
from clm_sync.sync import sync_config
from clm_sync.models import FetchResult


def fake_transport(route: dict[str, object], api_key_sink: list[object] | None = None):
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

    def test_dot_preserved_in_version_and_date_tokens(self):
        self.assertEqual("Agnes 2.0 Flash", base_display_name("agnes-2.0-flash"))
        self.assertEqual("QWEN3.5 72b", base_display_name("qwen3.5-72b"))
        self.assertEqual("V4.5", base_display_name("v4.5"))
        self.assertEqual("Glm 4.6", base_display_name("glm-4.6"))
        self.assertEqual(
            "Model 2024.08.06", base_display_name("model-2024.08.06")
        )

    def test_dot_in_prefix_and_plain_dotted_word(self):
        self.assertEqual(
            "Inner Segment",
            base_display_name("something.with.dots/inner-segment"),
        )
        self.assertEqual("Foo.bar.baz", base_display_name("foo.bar.baz"))


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

    def test_new_model_keys_ordered_id_first_with_remote_name(self):
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
        self.assertEqual(
            [
                "id",
                "name",
                "url",
                "toolCalling",
                "vision",
                "maxInputTokens",
                "maxOutputTokens",
                "supportsReasoningEffort",
            ],
            list(added.keys()),
        )

    def test_new_model_keys_ordered_id_first_without_remote_name(self):
        cfg = [provider(models=[{"id": "old", "name": "Old", "url": "https://x.test"}])]
        route = {"https://x.test/v1/models": body("old", "new-model")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))
        added = next(
            model
            for model in outcome.config[0]["models"]
            if model["id"] == "new-model"
        )
        self.assertEqual(
            [
                "id",
                "name",
                "url",
                "toolCalling",
                "vision",
                "maxInputTokens",
                "maxOutputTokens",
                "supportsReasoningEffort",
            ],
            list(added.keys()),
        )

    def test_existing_model_key_order_preserved(self):
        seed = {"name": "Old", "id": "old", "url": "https://x.test"}
        cfg = [provider(models=[copy.deepcopy(seed)])]
        route = {"https://x.test/v1/models": body("old")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))
        kept = outcome.config[0]["models"][0]
        self.assertEqual(["name", "id", "url"], list(kept.keys()))

    def test_new_model_without_remote_name_uses_dot_preserving_fallback(self):
        cfg = [provider(models=[{"id": "old", "name": "Old", "url": "https://x.test"}])]
        route = {"https://x.test/v1/models": body("old", "agnes-2.0-flash")}
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, route))
        added = next(
            model
            for model in outcome.config[0]["models"]
            if model["id"] == "agnes-2.0-flash"
        )
        self.assertEqual("Agnes 2.0 Flash", added["name"])

    def test_new_model_with_dotted_remote_name_is_kept_verbatim(self):
        payload = json.dumps(
            {
                "data": [
                    {
                        "id": "agnes-2.0-flash",
                        "name": "Agnes 2.0 Flash",
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
            model
            for model in outcome.config[0]["models"]
            if model["id"] == "agnes-2.0-flash"
        )
        self.assertEqual("Agnes 2.0 Flash", added["name"])

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

    def test_cross_provider_settings_pruned_when_model_removed(self):
        """A non-customendpoint provider that references a removed model via a
        three-segment settings key must have that key pruned; its own
        ``models`` list and unrelated settings keys are left untouched."""
        source = provider(
            name="Agnes",
            models=[{"id": "m1", "name": "M1", "url": "https://x.test"}],
        )
        other = {
            "name": "Copilot",
            "vendor": "agent-host-copilotcli",
            "models": [{"id": "x", "name": "X", "url": "https://a.test"}],
            "settings": {
                "customendpoint/Agnes/m1": {"thinkingLevel": "max"},
                "auto": {"tier": "x"},
            },
        }
        # Remote succeeds but only advertises m2, so m1 is genuinely removed
        # (an empty data list would be a failed fetch, not a deletion).
        route = {"https://x.test/v1/models": body("m2")}
        outcome = sync_config([source, other], lambda n, u: _fetch(n, u, route))

        self.assertEqual(["m1"], outcome.providers[0].removed)
        # The cross-provider reference is pruned; the unrelated key survives.
        self.assertEqual({"auto": {"tier": "x"}}, outcome.config[1]["settings"])
        self.assertEqual(["customendpoint/Agnes/m1"],
                         outcome.providers[0].cross_settings_removed)
        # The other provider's own models list is never touched.
        self.assertEqual([{"id": "x", "name": "X", "url": "https://a.test"}],
                         outcome.config[1]["models"])

    def test_cross_provider_settings_not_pruned_with_no_delete(self):
        """With --no-delete the model is kept, so nothing is removed and the
        cross-provider settings reference must survive untouched."""
        source = provider(
            name="Agnes",
            models=[{"id": "m1", "name": "M1", "url": "https://x.test"}],
        )
        other = {
            "name": "Copilot",
            "vendor": "agent-host-copilotcli",
            "settings": {"customendpoint/Agnes/m1": {"thinkingLevel": "max"}},
        }
        route = {"https://x.test/v1/models": body("m2")}
        outcome = sync_config(
            [source, other],
            lambda n, u: _fetch(n, u, route),
            allow_delete=False,
        )
        self.assertEqual([], outcome.providers[0].removed)
        self.assertEqual([], outcome.providers[0].cross_settings_removed)
        self.assertEqual(
            {"customendpoint/Agnes/m1": {"thinkingLevel": "max"}},
            outcome.config[1]["settings"],
        )

    def test_cross_provider_settings_pruned_for_slashed_model_id(self):
        """A model id containing '/' must still match exactly, because the
        qualified key is built by f-string concatenation and compared by full
        equality (never split on '/').  Pins the 'exact concatenation'
        assumption documented in prune_cross_settings."""
        model_id = "nvidia/nemotron-3-ultra-550b-a55b"
        cross_key = "customendpoint/NvidiaProvider/" + model_id
        source = provider(
            name="NvidiaProvider",
            models=[{"id": model_id, "name": "Nemotron", "url": "https://x.test"}],
        )
        other = {
            "name": "Copilot",
            "vendor": "agent-host-copilotcli",
            "settings": {cross_key: {"thinkingLevel": "max"}},
        }
        # Remote succeeds with a different model, so the slashed id is removed.
        route = {"https://x.test/v1/models": body("m2")}
        outcome = sync_config([source, other], lambda n, u: _fetch(n, u, route))
        self.assertEqual([model_id], outcome.providers[0].removed)
        self.assertNotIn("settings", outcome.config[1])
        self.assertEqual([cross_key],
                         outcome.providers[0].cross_settings_removed)

    def test_cross_provider_settings_idempotent(self):
        """After the cross-provider reference is pruned on the first pass, a
        second run over the result is a no-op: nothing is removed and the
        config is reported unchanged."""
        source = provider(
            name="Agnes",
            models=[{"id": "m1", "name": "M1", "url": "https://x.test"}],
        )
        other = {
            "name": "Copilot",
            "vendor": "agent-host-copilotcli",
            "settings": {"customendpoint/Agnes/m1": {"thinkingLevel": "max"}},
        }
        # Fixed route: the remote succeeds but only advertises m2 on both
        # runs, so m1 is removed on the first and is NOT re-added on the
        # second.  Reusing this same fetcher is deliberate: if the second
        # run re-advertised m1, the added model would flip `changed` to True
        # and the no-op assertion below would falsely fail.
        route = {"https://x.test/v1/models": body("m2")}
        fetcher = lambda n, u: _fetch(n, u, route)

        o1 = sync_config([source, other], fetcher)
        self.assertEqual(["customendpoint/Agnes/m1"],
                         o1.providers[0].cross_settings_removed)
        self.assertNotIn("settings", o1.config[1])

        # Second pass reuses the SAME fetcher (still no m1), so the only thing
        # that could change is a re-adding of m1, which the assertions below
        # rule out.
        o2 = sync_config(o1.config, fetcher)
        self.assertFalse(o2.changed, "second run must be a no-op")
        self.assertEqual([], o2.providers[0].removed)
        self.assertEqual([], o2.providers[0].cross_settings_removed)

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

    def test_url_only_pointer_survives_failed_fetch(self):
        """A provider whose only model entry is a url-only pointer (no id): when
        the fetch fails, the pointer must be re-added so a later run can still
        reach the endpoint, instead of being discarded as an invalid entry and
        losing the endpoint forever.  The failed fetch is still reported."""
        cfg = [provider(models=[{"url": "https://x.test"}])]
        outcome = sync_config(cfg, lambda n, u: _fetch(n, u, {}))  # no fixture -> 404
        # The url-only entry was discarded as invalid...
        self.assertEqual([{"url": "https://x.test"}], outcome.providers[0].discarded_invalid)
        # ...but the endpoint pointer is re-added so it stays resync-able.
        self.assertEqual([{"url": "https://x.test"}], outcome.config[0]["models"])
        self.assertFalse(outcome.changed, "pointer re-added identically -> no rewrite")
        self.assertTrue(outcome.providers[0].errors, "the failed fetch must be reported")

    def test_provider_without_any_endpoint_is_flagged(self):
        """A customendpoint that declares no models and no url must be surfaced
        via the no_endpoints flag (a warning in the report) instead of silently
        doing nothing.  It does not count as an error, and no `models` key is
        fabricated on it."""
        cfg = [{"name": "NoEp", "vendor": "customendpoint", "apiType": "chat-completions"}]
        outcome = sync_config(cfg, lambda n, u: FetchResult(u, n, True, []))
        self.assertTrue(outcome.providers[0].no_endpoints)
        self.assertTrue(outcome.providers[0].ok, "no request was made, so no error")
        self.assertNotIn("models", outcome.config[0])
        self.assertFalse(outcome.changed)

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

        def fetcher(*args: object):
            # args == (name, base_url, api_key).  Guard each index on the
            # argument count so the access is provably in-bounds.
            seen_keys.append(args[2] if len(args) > 2 else None)
            name = args[0] if len(args) > 0 else ""
            base_url = args[1] if len(args) > 1 else ""
            return _fetch(name, base_url, {"https://x.test/v1/models": body("seed")})

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


class CreditsTest(unittest.TestCase):
    """Balance lookups only for *known* hosts; everything else is omitted."""

    def test_only_known_hosts_have_balance_endpoint(self):
        self.assertTrue(has_credits_endpoint("https://api.deepseek.com"))
        self.assertTrue(has_credits_endpoint("https://api.deepseek.com/v1"))
        self.assertTrue(has_credits_endpoint("https://openrouter.ai/v1/chat/completions"))
        # Unknown gateway hosts are NOT queried.  The negative fixtures use
        # synthetic names on purpose: this test file must never name a real
        # third-party gateway.
        self.assertFalse(has_credits_endpoint("https://unknown-gateway.example.com"))
        self.assertFalse(has_credits_endpoint("https://x.test"))

    def test_fetch_credits_unknown_host_is_none_without_network(self):
        # A transport that raises proves we never hit the network for unknown
        # hosts (the lookup is skipped before any request is made).
        def boom(url, timeout=None, api_key=None):
            raise AssertionError("must not fetch for unknown host")

        self.assertIsNone(
            fetch_credits("P", "https://unknown-gateway.example.com", transport=boom)
        )

    def test_fetch_credits_known_host_success(self):
        def ok(url, timeout=None, api_key=None):
            self.assertEqual(url, "https://api.deepseek.com/user/balance")
            return (
                '{"is_available": true, "balance_infos": '
                '[{"currency": "CNY", "total_balance": "1.23"}]}'
            )

        self.assertEqual(
            "1.23 CNY", fetch_credits("DeepSeek", "https://api.deepseek.com", transport=ok)
        )

    def test_fetch_credits_failure_is_none(self):
        def boom(url, timeout=None, api_key=None):
            raise TransportError("HTTP 500")

        self.assertIsNone(
            fetch_credits("DeepSeek", "https://api.deepseek.com", transport=boom)
        )

    def test_parse_credits_payload_known_fields(self):
        spec = _CREDITS_ENDPOINTS["api.deepseek.com"]
        self.assertEqual(
            "1.23 CNY",
            parse_credits_payload(
                '{"balance_infos": [{"total_balance": "1.23"}]}', spec
            ),
        )

    def test_parse_credits_payload_fallback_when_fields_missing(self):
        # Valid JSON, but none of the declared fields is present (the vendor
        # reshaped its response): a truncated raw body is returned so the
        # operator can see what changed.
        spec = _CREDITS_ENDPOINTS["api.deepseek.com"]
        raw = '{"unexpected_field": "' + "x" * 200 + '"}'
        out = parse_credits_payload(raw, spec)
        self.assertIsInstance(out, str, "fallback must yield a string")
        # Short-circuit on isinstance so the optional `out` is provably a
        # `str` before `len` / `endswith` are touched (assertIsInstance does
        # not narrow the type for the checker).
        self.assertTrue(isinstance(out, str) and len(out) <= 123, "fallback must truncate")
        self.assertTrue(isinstance(out, str) and out.endswith("..."))

    def test_parse_credits_payload_non_json_is_none(self):
        # A 200 + HTML error page (or gateway message) is not a balance: it
        # must not be echoed into the report as if it were one.
        spec = _CREDITS_ENDPOINTS["api.deepseek.com"]
        self.assertIsNone(parse_credits_payload("<html>Gateway Timeout</html>", spec))

    def test_parse_credits_payload_multi_field_is_labelled(self):
        # openrouter declares two fields: every surviving value carries its
        # own label, so a missing metric can never be mis-read as the other.
        spec = _CREDITS_ENDPOINTS["openrouter.ai"]
        self.assertEqual(
            "credits: 500 USD, used: 5.0 USD",
            parse_credits_payload(
                '{"data": {"total_credits": 500, "total_usage": 500}}', spec
            ),
        )
        # Only one metric present: still labelled, because the spec declares
        # two fields (a bare "5.0 USD" would be ambiguous).
        self.assertEqual(
            "used: 5.0 USD",
            parse_credits_payload('{"data": {"total_usage": 500}}', spec),
        )

    def test_parse_credits_payload_string_number_is_scaled(self):
        # Vendors that report the number as a string ("10000") must still
        # have the spec's scale applied, not silently skip it.
        spec = _CREDITS_ENDPOINTS["api.novita.ai"]
        self.assertEqual(
            "1.0 USD",
            parse_credits_payload('{"availableBalance": "10000"}', spec),
        )

    def test_parse_credits_payload_empty_is_none(self):
        spec = _CREDITS_ENDPOINTS["api.deepseek.com"]
        self.assertIsNone(parse_credits_payload("   ", spec))

    def test_json_path_index_only_accepts_ascii_decimal(self):
        """Point 2: a bracketed index is a valid path part only when it is a
        plain ASCII decimal integer.  Unicode digits like '2' are rejected
        even though str.isdigit accepts them, because int('2') would raise."""
        payload = {"data": [{"total_balance": "1.00"}, {"total_balance": "2.00"}]}
        # ASCII decimal indices resolve.
        self.assertTrue(_json_path(payload, "data[0].total_balance")[0])
        self.assertEqual("1.00", _json_path(payload, "data[0].total_balance")[1])
        self.assertEqual("2.00", _json_path(payload, "data[1].total_balance")[1])
        # Unicode superscript two: isdigit() is True but int() raises, so the
        # path must be rejected (found=False), not crash.
        self.assertFalse(_json_path(payload, "data[\u00b2]")[0])
        # Other malformed forms are likewise rejected, never silently ignored.
        self.assertFalse(_json_path(payload, "data[]")[0])
        self.assertFalse(_json_path(payload, "data[-1]")[0])
        self.assertFalse(_json_path(payload, "data[1.5]")[0])
        self.assertFalse(_json_path(payload, "data[abc]")[0])

    def test_literal_api_key_reaches_credits_lookup(self):
        """A2: the credits path must reuse the same key resolution as model
        sync — a literal apiKey reaches fetch_credits verbatim (previously the
        CLI sent a keyless request here, and a missing apiKey would even raise
        AttributeError on None)."""
        cfg = [
            provider(
                name="P",
                models=[{"id": "a", "url": "https://api.deepseek.com"}],
                api_key="sk-literal",
            )
        ]
        # The remote still advertises the seeded model, so the synced config
        # keeps its endpoint urls (an empty success would trigger deletion).
        outcome = sync_config(cfg, lambda n, u: FetchResult(u, n, True, ["a"]))
        seen: list = []

        def recorder(name, base_url, timeout=None, api_key=None):
            seen.append(api_key)
            return "1.23 CNY"

        with patch("clm_sync.cli.fetch_credits", side_effect=recorder):
            cli.enrich_with_credits(outcome, timeout=1, key_resolver=None, targets_all=True)

        self.assertEqual(["sk-literal"], seen)
        self.assertEqual("1.23 CNY", outcome.providers[0].credits)

    def test_all_mode_pairs_duplicate_names_by_object(self):
        """A4: under --all, each result is paired with its own provider object
        so two same-named providers never mix up their balance lookup.  The
        buggy name-keyed lookup would have fetched the *first* provider's host
        and key for both."""
        cfg = [
            provider(
                name="Dup",
                models=[{"id": "a", "url": "https://api.deepseek.com"}],
                api_key="sk-deepseek",
            ),
            provider(
                name="Dup",
                models=[{"id": "b", "url": "https://api.moonshot.cn"}],
                api_key="sk-moonshot",
            ),
        ]

        def keep(name, url):
            # Each remote keeps advertising its own model, so both providers
            # keep their endpoint urls in the synced config.
            ids = ["a"] if "deepseek" in url else ["b"]
            return FetchResult(base_url=url, provider_name=name, success=True, model_ids=ids)

        outcome = sync_config(cfg, keep)

        calls: list = []

        def recorder(name, base_url, timeout=None, api_key=None):
            calls.append((base_url, api_key))
            return "1.23 CNY"

        with patch("clm_sync.cli.fetch_credits", side_effect=recorder):
            cli.enrich_with_credits(outcome, timeout=1, key_resolver=None, targets_all=True)

        # Each provider looked up its OWN host and key, in provider order.
        self.assertEqual(
            [
                ("https://api.deepseek.com", "sk-deepseek"),
                ("https://api.moonshot.cn", "sk-moonshot"),
            ],
            calls,
        )
        self.assertEqual("1.23 CNY", outcome.providers[0].credits)
        self.assertEqual("1.23 CNY", outcome.providers[1].credits)

    def test_all_mode_pairing_does_not_depend_on_provider_order(self):
        """Point 3: pairing is by each result's config_index, not by the list
        order of outcome.providers.  Reversing the result list (as a future
        reordering of sync_config's appends might do) must not shift which
        provider a result's balance comes from."""
        cfg = [
            provider(
                name="A",
                models=[{"id": "a", "url": "https://api.deepseek.com"}],
                api_key="sk-a",
            ),
            provider(
                name="B",
                models=[{"id": "b", "url": "https://api.moonshot.cn"}],
                api_key="sk-b",
            ),
        ]

        def keep(name, url):
            ids = ["a"] if "deepseek" in url else ["b"]
            return FetchResult(base_url=url, provider_name=name, success=True, model_ids=ids)

        outcome = sync_config(cfg, keep)
        # Each result carries its own config_index (0 for A, 1 for B).
        self.assertEqual([0, 1], [r.config_index for r in outcome.providers])

        # Simulate a future reordering of the results list.
        outcome.providers.reverse()

        def recorder(name, base_url, timeout=None, api_key=None):
            # A distinguishable value keyed by host, so a shifted pairing
            # (A's result pulling B's balance) would be caught.
            return "deepseek" if "deepseek" in base_url else "moonshot"

        with patch("clm_sync.cli.fetch_credits", side_effect=recorder):
            cli.enrich_with_credits(outcome, timeout=1, key_resolver=None, targets_all=True)

        # A's result (config_index 0) must hold deepseek's value and B's
        # result (config_index 1) must hold moonshot's, regardless of the
        # (now reversed) list order.
        by_index = {r.config_index: r for r in outcome.providers}
        self.assertEqual("deepseek", by_index[0].credits)
        self.assertEqual("moonshot", by_index[1].credits)


class ColoringTest(unittest.TestCase):
    """Decision matrix for _stdout_color_enabled / _windows_vt100_ready.

    Patches the individual signals (os.name, isatty, NO_COLOR, the VT100
    probe) so each branch is exercised deterministically, without needing a
    real console handle."""

    def test_no_color_env_disables_color(self):
        with patch.dict(os.environ, {"NO_COLOR": "1"}), patch.object(
            sys.stdout, "isatty", return_value=True
        ), patch("clm_sync.cli.os.name", "nt"):
            self.assertFalse(cli._stdout_color_enabled())

    def test_non_tty_stdout_is_plain(self):
        with patch.object(sys.stdout, "isatty", return_value=False), patch(
            "clm_sync.cli.os.name", "nt"
        ), patch.dict(os.environ, {}):
            self.assertFalse(cli._stdout_color_enabled())

    def test_non_windows_tty_stays_coloured(self):
        # Non-Windows keeps the pre-existing behaviour: TTY -> colour, with
        # no VT probe involved at all.
        with patch.object(sys.stdout, "isatty", return_value=True), patch(
            "clm_sync.cli.os.name", "posix"
        ), patch("clm_sync.cli._windows_vt100_ready") as probe, patch.dict(
            os.environ, {}
        ):
            self.assertTrue(cli._stdout_color_enabled())
            probe.assert_not_called(), "non-Windows must not run the VT probe"

    def test_windows_tty_with_vt_ready_is_coloured(self):
        with patch.object(sys.stdout, "isatty", return_value=True), patch(
            "clm_sync.cli.os.name", "nt"
        ), patch("clm_sync.cli._windows_vt100_ready", return_value=True) as probe, patch.dict(
            os.environ, {}
        ):
            self.assertTrue(cli._stdout_color_enabled())
            probe.assert_called_once()

    def test_windows_tty_without_vt_falls_back_to_plain(self):
        # The original bug: a real conhost with VT off must NOT dump raw
        # escape codes.  Probe returns False -> no colour.
        with patch.object(sys.stdout, "isatty", return_value=True), patch(
            "clm_sync.cli.os.name", "nt"
        ), patch("clm_sync.cli._windows_vt100_ready", return_value=False), patch.dict(
            os.environ, {}
        ):
            self.assertFalse(cli._stdout_color_enabled())

    def test_windows_non_real_console_is_trusted_to_render(self):
        # A non-conhost handle (GetConsoleMode fails, e.g. VS Code integrated
        # terminal) is trusted to render ANSI, so colour stays on.
        self.assertTrue(cli._windows_vt100_ready())

    def test_render_report_toggles_ansi_by_color_flag(self):
        # Locks the render layer: color=True injects ANSI escapes, color=False
        # (the stderr/plain copy) emits none.
        from clm_sync.models import ProviderSyncResult

        outcome = type(
            "O", (), {"providers": [ProviderSyncResult(name="P", changed=True, added=["a"])]}
        )()
        self.assertIn("\x1b[", cli.render_report(outcome, color=True))
        self.assertNotIn("\x1b[", cli.render_report(outcome, color=False))


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
            ), patch("clm_sync.cli.fetch_credits", return_value="100 USD"):
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
                patch("clm_sync.cli.fetch_credits", return_value=None),
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
