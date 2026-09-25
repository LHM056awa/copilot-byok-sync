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


def fake_transport(route: dict[str, object]):
    """Build a transport keyed by requested url.

    route values are either a body string or an Exception to raise.
    """

    def _call(url: str, timeout=None) -> str:
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
        self.assertEqual(["new-model", "old"], [m["id"] for m in models])
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
        cfg = [provider(api_key="${input:test-secret}")]
        original = copy.deepcopy(cfg)
        route = {"https://x.test/v1/models": body("brand-new")}
        sync_config(
            [
                provider(
                    models=[{"id": "seed", "name": "S", "url": "https://x.test"}],
                    api_key="${input:test-secret}",
                )
            ],
            lambda n, u: _fetch(n, u, route),
        )
        updated = provider(
            models=[{"id": "seed", "name": "S", "url": "https://x.test"}],
            api_key="${input:test-secret}",
        )
        for field in ("name", "vendor", "apiKey", "apiType"):
            self.assertEqual(original[0][field], updated[field])

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

            def failing(name, base_url, timeout=None):
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
