"""Concurrency tests for endpoint requests (models + balance lookups).

Everything here is offline: requests go through injected fetchers / fake
transports that sleep to scramble completion order.  The point is to prove
that concurrency only changes *how fast* results arrive, never *what* the
sync produces.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from clm_sync import cli
from clm_sync.client import TransportError, fetch_models
from clm_sync.config import serialize
from clm_sync.models import FetchResult
from clm_sync.sync import (
    DEFAULT_MAX_WORKERS,
    resolve_worker_count,
    run_ordered,
    sync_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def body(*ids: str) -> str:
    return json.dumps({"data": [{"id": i} for i in ids]})


def provider(name, models, settings=None, api_key="${input:chat.lm.secret.x}"):
    entry = {
        "name": name,
        "vendor": "customendpoint",
        "apiKey": api_key,
        "apiType": "chat-completions",
        "models": models,
    }
    if settings is not None:
        entry["settings"] = settings
    return entry


def rich_config():
    """A config that exercises every report line and the delete guard.

    * Alpha  -- one healthy url (adds + removes + settings pruning) and one
                url that only advertises media models (``no text signal``).
    * Beta   -- its only url returns 401 -> deletion skipped, ``error:`` line.
    * Gamma  -- two urls; the second one's fetcher *raises* -> isolated.
    * Dup x2 -- same provider name, different urls/keys (must not mix).
    * Junk   -- carries invalid entries (``discarded invalid entries``).
    * Empty  -- no endpoint url at all (``warning: no endpoint url``).
    * Copilot (built-in vendor) -- holds a cross-provider settings key that
      points at a model Alpha removes, and must never have its models touched.
    """
    return [
        provider(
            "Alpha",
            [
                {"id": "a-keep", "name": "A Keep", "url": "https://a1.test"},
                {"id": "a-stale", "name": "A Stale", "url": "https://a1.test"},
                {"id": "a-local", "name": "A Local", "url": "https://a2.test"},
            ],
            settings={"a-stale": {"reasoningEffort": "high"}, "a-keep": {"x": 1}},
            api_key="sk-alpha",
        ),
        provider(
            "Beta",
            [{"id": "b-old", "name": "B Old", "url": "https://b1.test"}],
            api_key="sk-beta",
        ),
        provider(
            "Gamma",
            [
                {"id": "g-one", "name": "G One", "url": "https://g1.test"},
                {"id": "g-two", "name": "G Two", "url": "https://g2-boom.test"},
            ],
        ),
        provider("Dup", [{"id": "d1", "url": "https://dup1.test"}], api_key="sk-d1"),
        provider("Dup", [{"id": "d2", "url": "https://dup2.test"}], api_key="sk-d2"),
        provider(
            "Junk",
            [{"name": "no id", "url": "https://j1.test"}, {"id": "j-ok", "url": "https://j1.test"}, 42],
        ),
        provider("Empty", []),
        {
            "name": "Copilot",
            "vendor": "copilot",
            "models": [{"id": "builtin"}],
            "settings": {
                "customendpoint/Alpha/a-stale": {"reasoningEffort": "max"},
                "customendpoint/Alpha/a-keep": {"reasoningEffort": "low"},
            },
        },
    ]


ROUTES = {
    "https://a1.test/v1/models": body("a-keep", "a-new-2", "a-new-1", "a-image-gen"),
    "https://a2.test/v1/models": body("sora-video", "whisper-audio"),
    "https://b1.test/v1/models": TransportError("HTTP 401"),
    "https://g1.test/v1/models": body("g-one", "g-new"),
    "https://dup1.test/v1/models": body("d1", "d1-new"),
    "https://dup2.test/v1/models": body("d2", "d2-new"),
    "https://j1.test/v1/models": body("j-ok", "j-new"),
}

#: Healthy variant of ROUTES for CLI tests (failures are injected per test).
CLI_ROUTES = {**ROUTES, "https://b1.test/v1/models": body("b-old")}

#: Planned-call order for rich_config() (providers in config order, urls in
#: model order).  Used to give *later* calls *shorter* delays so completion
#: order is the reverse of submission order.
PLANNED_URLS = [
    "https://a1.test",
    "https://a2.test",
    "https://b1.test",
    "https://g1.test",
    "https://g2-boom.test",
    "https://dup1.test",
    "https://dup2.test",
    "https://j1.test",
]


def _delay_for(base_url: str, scale: float = 0.01) -> float:
    try:
        idx = PLANNED_URLS.index(base_url)
    except ValueError:
        return 0.0
    return (len(PLANNED_URLS) - idx) * scale


def make_fetcher(*, delay_scale=0.01, log=None, lock=None):
    """Three-argument fetcher (the CLI shape) over ROUTES.

    Later-planned calls sleep less, so with >1 worker they finish first.
    ``g2-boom`` raises from the fetcher itself (not via the transport).
    """

    def transport(url, timeout=None, api_key=None):
        entry = ROUTES.get(url)
        if entry is None:
            raise TransportError(f"HTTP 404 (no fixture for {url})")
        if isinstance(entry, Exception):
            raise entry
        return entry

    def fetcher(name, base_url, api_key):
        time.sleep(_delay_for(base_url, delay_scale))
        if log is not None:
            with lock:
                log.append((name, base_url, api_key))
        if "boom" in base_url:
            raise RuntimeError("worker exploded")
        return fetch_models(name, base_url, timeout=1.0, transport=transport, api_key=api_key)

    return fetcher


def snapshot(outcome):
    """Everything observable about an outcome, for equality checks."""
    return (
        cli.render_report(outcome, color=False),
        json.dumps(outcome.config, ensure_ascii=False, sort_keys=False),
        outcome.changed,
        [(p.name, p.ok, p.config_index) for p in outcome.providers],
    )


def write_cfg(tmp, cfg):
    path = os.path.join(tmp, "chatLanguageModels.json")
    Path(path).write_text(serialize(cfg), encoding="utf-8")
    return path


def cli_fetch_models(*, delay=0.0, counter=None, lock=None, fail_urls=(), raise_urls=()):
    """Stand-in for clm_sync.cli.fetch_models (keyword timeout/api_key)."""

    def _fake(name, base_url, timeout=None, api_key=None):
        time.sleep(delay)
        try:
            if base_url in raise_urls:
                raise RuntimeError("boom")
            if base_url in fail_urls:
                return FetchResult(base_url=base_url, provider_name=name, success=False, error="HTTP 401")
            return fetch_models(
                name,
                base_url,
                transport=lambda url, t=None, k=None: CLI_ROUTES[url],
            )
        finally:
            if counter is not None:
                with lock:
                    counter[0] += 1

    return _fake


# ---------------------------------------------------------------------------
# run_ordered / resolve_worker_count
# ---------------------------------------------------------------------------


class RunOrderedTest(unittest.TestCase):
    def test_results_follow_submission_order_not_completion_order(self):
        finished = []
        lock = threading.Lock()

        def task(i):
            def _run():
                time.sleep((5 - i) * 0.02)  # last submitted finishes first
                with lock:
                    finished.append(i)
                return i

            return _run

        out = run_ordered([task(i) for i in range(5)], 5)
        self.assertEqual([0, 1, 2, 3, 4], out)
        # Sanity: completion order really was scrambled.
        self.assertNotEqual([0, 1, 2, 3, 4], finished)

    def test_single_worker_runs_inline_in_calling_thread(self):
        threads = []
        run_ordered([lambda: threads.append(threading.current_thread())] * 3, 1)
        self.assertEqual([threading.current_thread()] * 3, threads)

    def test_peak_concurrency_respects_bound(self):
        state = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def task():
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1

        run_ordered([task] * 10, 3)
        self.assertEqual(3, state["peak"])

    def test_resolve_worker_count(self):
        self.assertEqual(1, resolve_worker_count(None, 0))
        self.assertEqual(3, resolve_worker_count(None, 3))
        self.assertEqual(DEFAULT_MAX_WORKERS, resolve_worker_count(None, 100))
        self.assertEqual(2, resolve_worker_count(16, 2))  # no idle threads
        self.assertEqual(4, resolve_worker_count(4, 10))
        with self.assertRaises(ValueError):
            resolve_worker_count(0, 5)


# ---------------------------------------------------------------------------
# sync_config
# ---------------------------------------------------------------------------


class DeterminismTest(unittest.TestCase):
    def test_report_and_config_identical_for_any_worker_count(self):
        cfg = rich_config()
        baseline = snapshot(sync_config(cfg, make_fetcher(), max_workers=1))
        for workers in (1, 2, 4, 8, None):
            for _ in range(5):
                with self.subTest(workers=workers):
                    outcome = sync_config(cfg, make_fetcher(), max_workers=workers)
                    self.assertEqual(baseline, snapshot(outcome))

    def test_input_config_is_not_mutated(self):
        cfg = rich_config()
        before = copy.deepcopy(cfg)
        sync_config(cfg, make_fetcher(), max_workers=8)
        self.assertEqual(before, cfg)

    def test_fixture_exercises_every_contract(self):
        """Guard the fixture itself so the determinism test stays meaningful."""
        outcome = sync_config(rich_config(), make_fetcher(), max_workers=8)
        report = cli.render_report(outcome)
        by_name = {(p.name, p.config_index): p for p in outcome.providers}
        alpha = by_name[("Alpha", 0)]
        self.assertEqual(["a-stale"], alpha.removed)
        self.assertEqual(["a-new-1", "a-new-2"], alpha.added)
        self.assertEqual(["a-stale"], alpha.settings_keys_removed)
        self.assertEqual(["customendpoint/Alpha/a-stale"], alpha.cross_settings_removed)
        self.assertEqual(["https://a2.test"], alpha.no_signal_endpoints)
        self.assertTrue(alpha.skipped_deletion)  # a2 had no text signal
        # New models are appended in the order the endpoint advertised them.
        self.assertEqual(
            ["a-keep", "a-local", "a-new-2", "a-new-1"],
            [m["id"] for m in outcome.config[0]["models"]],
        )
        # Beta: 401 -> nothing deleted.
        self.assertEqual([], by_name[("Beta", 1)].removed)
        self.assertIn("error: https://b1.test: HTTP 401", report)
        # Gamma: raising fetcher isolated into an error line; g-two kept.
        gamma = by_name[("Gamma", 2)]
        self.assertEqual(["https://g2-boom.test: unexpected error: worker exploded"], gamma.errors)
        self.assertIn("g-two", [m["id"] for m in outcome.config[2]["models"]])
        self.assertEqual(["g-new"], gamma.added)
        # Same-named providers keep their own results.
        self.assertEqual(["d1-new"], by_name[("Dup", 3)].added)
        self.assertEqual(["d2-new"], by_name[("Dup", 4)].added)
        self.assertIn("discarded invalid entries", report)
        self.assertIn("warning: no endpoint url", report)
        # Built-in vendor: models untouched, only the cross key pruned.
        copilot = outcome.config[7]
        self.assertEqual([{"id": "builtin"}], copilot["models"])
        self.assertEqual(
            {"customendpoint/Alpha/a-keep": {"reasoningEffort": "low"}}, copilot["settings"]
        )


class SpeedupTest(unittest.TestCase):
    N = 6
    DELAY = 0.2

    def _cfg(self):
        return [
            provider(f"P{i}", [{"id": f"m{i}", "url": f"https://p{i}.test"}])
            for i in range(self.N)
        ]

    def _fetcher(self, name, base_url, api_key):
        time.sleep(self.DELAY)
        return FetchResult(base_url=base_url, provider_name=name, success=True, model_ids=[name.lower().replace("p", "m")])

    def test_concurrent_wall_time_is_close_to_slowest_request(self):
        start = time.perf_counter()
        outcome = sync_config(self._cfg(), self._fetcher, max_workers=self.N)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"took {elapsed:.3f}s, serial would be {self.N * self.DELAY:.1f}s")
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.changed)

    def test_auto_worker_count_is_concurrent(self):
        start = time.perf_counter()
        sync_config(self._cfg(), self._fetcher, max_workers=None)
        self.assertLess(time.perf_counter() - start, 0.5)

    def test_single_worker_is_serial(self):
        start = time.perf_counter()
        sync_config(self._cfg(), self._fetcher, max_workers=1)
        self.assertGreaterEqual(time.perf_counter() - start, self.N * self.DELAY * 0.95)


class FailureIsolationTest(unittest.TestCase):
    def test_raising_fetcher_becomes_failed_result_for_any_worker_count(self):
        cfg = [
            provider("Ok", [{"id": "x", "url": "https://ok.test"}, {"id": "gone", "url": "https://ok.test"}]),
            provider("Bad", [{"id": "y", "url": "https://bad.test"}, {"id": "z", "url": "https://bad2.test"}]),
        ]

        def fetcher(name, base_url, api_key):
            if base_url == "https://bad.test":
                raise ValueError("kaboom")
            ids = {"https://ok.test": ["x"], "https://bad2.test": []}[base_url]
            return FetchResult(base_url=base_url, provider_name=name, success=True, model_ids=ids)

        for workers in (1, 4):
            with self.subTest(workers=workers):
                outcome = sync_config(cfg, fetcher, max_workers=workers)
                ok, bad = outcome.providers
                # Healthy provider still syncs (and may delete).
                self.assertEqual(["gone"], ok.removed)
                # Failing provider: error recorded, deletion blocked, models kept
                # even though its *other* endpoint returned an empty success.
                self.assertEqual(["https://bad.test: unexpected error: kaboom"], bad.errors)
                self.assertEqual([], bad.removed)
                self.assertTrue(bad.skipped_deletion)
                self.assertEqual(["y", "z"], [m["id"] for m in outcome.config[1]["models"]])
                self.assertFalse(outcome.ok)

    def test_exception_without_message_still_explains_itself(self):
        cfg = [provider("P", [{"id": "a", "url": "https://p.test"}])]

        def fetcher(name, base_url, api_key):
            raise TimeoutError()

        outcome = sync_config(cfg, fetcher, max_workers=4)
        self.assertEqual(["https://p.test: unexpected error: TimeoutError"], outcome.providers[0].errors)


class LegacyFetcherTest(unittest.TestCase):
    def _cfg(self):
        return [
            provider("P1", [{"id": "a", "url": "https://one.test"}], api_key="sk-1"),
            provider("P2", [{"id": "b", "url": "https://two.test"}], api_key="sk-2"),
            provider("P3", [{"id": "c", "url": "https://three.test"}, {"id": "d", "url": "https://four.test"}], api_key="sk-3"),
        ]

    def test_two_argument_fetcher_under_concurrency(self):
        seen = set()
        lock = threading.Lock()

        def fetcher(name, base_url):  # legacy contract: no api_key
            time.sleep(0.01)
            with lock:
                seen.add((name, base_url))
            return FetchResult(base_url=base_url, provider_name=name, success=True, model_ids=[])

        sync_config(self._cfg(), fetcher, max_workers=8)
        self.assertEqual(
            {
                ("P1", "https://one.test"),
                ("P2", "https://two.test"),
                ("P3", "https://three.test"),
                ("P3", "https://four.test"),
            },
            seen,
        )

    def test_var_positional_fetcher_receives_key_under_concurrency(self):
        seen = set()
        lock = threading.Lock()

        def fetcher(*args):
            with lock:
                seen.add(args)
            return FetchResult(base_url=args[1], provider_name=args[0], success=True, model_ids=[])

        sync_config(self._cfg(), fetcher, max_workers=8)
        self.assertIn(("P1", "https://one.test", "sk-1"), seen)
        self.assertIn(("P3", "https://four.test", "sk-3"), seen)
        self.assertEqual(4, len(seen))


class KeyResolutionTest(unittest.TestCase):
    def test_keys_resolved_once_per_provider_in_main_thread_before_any_fetch(self):
        cfg = [
            provider("P1", [{"id": "a", "url": "https://p1a.test"}, {"id": "b", "url": "https://p1b.test"}], api_key="${input:chat.lm.secret.one}"),
            provider("P2", [{"id": "c", "url": "https://p2.test"}], api_key="${input:chat.lm.secret.two}"),
            provider("P3", [{"id": "d", "url": "https://p3.test"}], api_key="${input:chat.lm.secret.bad}"),
        ]
        events = []
        lock = threading.Lock()
        main = threading.current_thread()

        def resolver(raw):
            with lock:
                events.append(("resolve", raw, threading.current_thread() is main))
            return None if raw.endswith("bad}") else "real-" + raw.split(".")[-1].rstrip("}")

        keys = {}

        def fetcher(name, base_url, api_key):
            with lock:
                events.append(("fetch", base_url, threading.current_thread() is main))
                keys[base_url] = api_key
            return FetchResult(base_url=base_url, provider_name=name, success=True, model_ids=[])

        sync_config(cfg, fetcher, key_resolver=resolver, max_workers=4)

        resolves = [e for e in events if e[0] == "resolve"]
        fetches = [e for e in events if e[0] == "fetch"]
        self.assertEqual(3, len(resolves), "one resolution per provider")
        self.assertTrue(all(on_main for _, _, on_main in resolves))
        self.assertEqual(4, len(fetches))
        last_resolve = max(i for i, e in enumerate(events) if e[0] == "resolve")
        first_fetch = min(i for i, e in enumerate(events) if e[0] == "fetch")
        self.assertLess(last_resolve, first_fetch)
        # Fetches really happened off the main thread.
        self.assertTrue(all(not on_main for _, _, on_main in fetches))
        self.assertEqual("real-one", keys["https://p1a.test"])
        self.assertEqual("real-one", keys["https://p1b.test"])
        self.assertEqual("real-two", keys["https://p2.test"])
        # Unresolvable placeholder degrades to a keyless request, never the
        # placeholder text itself.
        self.assertIsNone(keys["https://p3.test"])

    def test_legacy_fetcher_never_triggers_key_resolution(self):
        cfg = [provider("P", [{"id": "a", "url": "https://p.test"}])]
        calls = []

        def resolver(raw):
            calls.append(raw)
            return "x"

        sync_config(
            cfg,
            lambda n, u: FetchResult(base_url=u, provider_name=n, success=True, model_ids=["a"]),
            key_resolver=resolver,
            max_workers=4,
        )
        self.assertEqual([], calls)


# ---------------------------------------------------------------------------
# Balance lookups
# ---------------------------------------------------------------------------


class CreditsConcurrencyTest(unittest.TestCase):
    HOSTS = [
        "https://api.deepseek.com",
        "https://api.moonshot.cn",
        "https://api.moonshot.ai",
        "https://api.stepfun.com",
    ]

    def _make_outcome(self):
        cfg = [
            provider(f"C{i}", [{"id": f"m{i}", "url": url}], api_key=f"sk-{i}")
            for i, url in enumerate(self.HOSTS)
        ] + [provider("NoBalance", [{"id": "n", "url": "https://unknown.test"}])]
        return sync_config(
            cfg,
            lambda n, u: FetchResult(base_url=u, provider_name=n, success=True, model_ids=[n.replace("C", "m") if n != "NoBalance" else "n"]),
        )

    def test_lookups_run_concurrently_and_land_on_the_right_provider(self):
        outcome = self._make_outcome()
        main = threading.current_thread()
        threads = []
        lock = threading.Lock()

        def fake(name, base_url, timeout=None, api_key=None):
            time.sleep(0.2 - 0.04 * int(name[1:]))  # scramble completion order
            with lock:
                threads.append(threading.current_thread() is main)
            return f"{name}:{api_key}:{base_url}"

        start = time.perf_counter()
        with patch("clm_sync.cli.fetch_credits", side_effect=fake):
            cli.enrich_with_credits(outcome, timeout=1, key_resolver=None, targets_all=True, workers=4)
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.35, f"credits took {elapsed:.3f}s")
        self.assertFalse(any(threads), "lookups must run in worker threads")
        for i, url in enumerate(self.HOSTS):
            self.assertEqual(f"C{i}:sk-{i}:{url}", outcome.providers[i].credits)
        self.assertIsNone(outcome.providers[-1].credits)  # unknown host: no line

    def test_results_identical_for_any_worker_count(self):
        def fake(name, base_url, timeout=None, api_key=None):
            time.sleep(0.01 * (4 - int(name[1:])))
            return None if name == "C2" else f"{name}-balance"

        reports = set()
        for workers in (1, 2, 4, None):
            outcome = self._make_outcome()
            with patch("clm_sync.cli.fetch_credits", side_effect=fake):
                cli.enrich_with_credits(outcome, timeout=1, key_resolver=None, targets_all=True, workers=workers)
            reports.add(cli.render_report(outcome))
        self.assertEqual(1, len(reports))
        self.assertIn("credits: unavailable", reports.pop())

    def test_keys_resolved_in_main_thread_and_crash_degrades_to_unavailable(self):
        outcome = self._make_outcome()
        main = threading.current_thread()
        resolver_threads = []

        def resolver(raw):
            resolver_threads.append(threading.current_thread() is main)
            return raw

        def fake(name, base_url, timeout=None, api_key=None):
            if name == "C1":
                raise RuntimeError("balance endpoint exploded")
            return "ok"

        with patch("clm_sync.cli.fetch_credits", side_effect=fake):
            cli.enrich_with_credits(outcome, timeout=1, key_resolver=resolver, targets_all=True, workers=4)
        self.assertEqual([True] * 4, resolver_threads)
        self.assertEqual(["ok", "unavailable", "ok", "ok"], [p.credits for p in outcome.providers[:4]])

    def test_url_fallback_chain_within_a_provider_stays_sequential(self):
        cfg = [
            provider(
                "Multi",
                [
                    {"id": "a", "url": "https://api.deepseek.com"},
                    {"id": "b", "url": "https://api.moonshot.cn"},
                    {"id": "c", "url": "https://api.stepfun.com"},
                ],
            )
        ]
        outcome = sync_config(
            cfg,
            lambda n, u: FetchResult(base_url=u, provider_name=n, success=True, model_ids=["a", "b", "c"]),
        )
        calls = []

        def fake(name, base_url, timeout=None, api_key=None):
            calls.append(base_url)
            return "second" if "moonshot" in base_url else None

        with patch("clm_sync.cli.fetch_credits", side_effect=fake):
            cli.enrich_with_credits(outcome, timeout=1, key_resolver=None, targets_all=True, workers=8)
        self.assertEqual(["https://api.deepseek.com", "https://api.moonshot.cn"], calls)
        self.assertEqual("second", outcome.providers[0].credits)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_cfg():
    return [
        provider("A", [{"id": "a-keep", "url": "https://a1.test"}, {"id": "a-stale", "url": "https://a1.test"}]),
        provider("B", [{"id": "b-old", "url": "https://b1.test"}]),
        provider("D", [{"id": "d1", "url": "https://dup1.test"}]),
        provider("G", [{"id": "g-one", "url": "https://g1.test"}]),
    ]


class CliConcurrencyTest(unittest.TestCase):
    def _run(self, argv, cfg, fake_models, *, credits=None):
        """Run the CLI with non-TTY stdout; return (code, stdout, stderr, file text, writes)."""
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cfg(tmp, cfg)
            real_write = cli.write_config_atomic
            writes = []

            def recording_write(p, data):
                writes.append(p)
                real_write(p, data)

            with contextlib.ExitStack() as stack:
                stack.enter_context(patch("clm_sync.cli.fetch_models", side_effect=fake_models))
                stack.enter_context(
                    patch("clm_sync.cli.fetch_credits", side_effect=credits or (lambda *a, **k: None))
                )
                stack.enter_context(patch("clm_sync.cli.write_config_atomic", side_effect=recording_write))
                stack.enter_context(patch.object(sys.stdout, "isatty", return_value=False))
                stack.enter_context(patch("sys.stdout", out))
                stack.enter_context(patch("sys.stderr", err))
                code = cli.run(["--config", path, *argv])
            text = Path(path).read_text(encoding="utf-8")
        return code, out.getvalue().replace(path, "<cfg>"), err.getvalue(), text, writes

    def test_output_is_identical_for_workers_1_4_8_and_default(self):
        results = set()
        for workers in ([], ["--workers", "1"], ["--workers", "4"], ["--workers", "8"]):
            code, out, err, text, writes = self._run(
                ["--all", *workers], _cli_cfg(), cli_fetch_models(delay=0.02, fail_urls={"https://b1.test"})
            )
            results.add((code, out, err, text, len(writes)))
        self.assertEqual(1, len(results))

    def test_failure_aggregation_exit_code_and_report(self):
        code, out, err, _text, writes = self._run(
            ["--all", "--workers", "4"],
            _cli_cfg(),
            cli_fetch_models(fail_urls={"https://b1.test"}, raise_urls={"https://g1.test"}),
        )
        self.assertEqual(cli.EXIT_PARTIAL_FAILURE, code)
        for name in ("A", "B", "D", "G"):
            self.assertRegex(out, rf"\[(changes|unchanged)\] {name}\n")
        self.assertIn("error: https://b1.test: HTTP 401", out)
        self.assertIn("error: https://g1.test: unexpected error: boom", out)
        self.assertIn("deletion skipped: keeping existing models", out)
        self.assertIn("removed (1): a-stale", out)  # healthy provider still syncs
        self.assertIn("2 with errors", out)
        # Non-TTY: the plain report is mirrored to stderr, uncoloured.
        self.assertIn("error: https://b1.test: HTTP 401", err)
        self.assertNotIn("\x1b[", err)
        self.assertEqual(1, len(writes))

    def test_single_write_happens_after_every_fetch(self):
        counter = [0]
        lock = threading.Lock()
        fetches_at_write = []
        total_calls = 4

        def check_write(path, data):
            with lock:
                fetches_at_write.append(counter[0])

        with tempfile.TemporaryDirectory() as tmp:
            path = write_cfg(tmp, _cli_cfg())
            with (
                patch("clm_sync.cli.fetch_models", side_effect=cli_fetch_models(delay=0.05, counter=counter, lock=lock)),
                patch("clm_sync.cli.fetch_credits", return_value=None),
                patch("clm_sync.cli.write_config_atomic", side_effect=check_write) as write,
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                code = cli.run(["--config", path, "--all", "--workers", "4"])
        self.assertEqual(cli.EXIT_OK, code)
        write.assert_called_once()
        self.assertEqual([total_calls], fetches_at_write)

    def test_dry_run_and_noop_never_write(self):
        with self.subTest("dry-run"):
            *_rest, writes = self._run(["--all", "--dry-run", "--workers", "4"], _cli_cfg(), cli_fetch_models())
            self.assertEqual([], writes)
        with self.subTest("no-op"):
            noop = [
                provider("A", [{"id": "a-keep", "url": "https://a1.test"}, {"id": "a-new-2", "url": "https://a1.test"},
                               {"id": "a-new-1", "url": "https://a1.test"}]),
            ]
            code, out, _err, _text, writes = self._run(["--all", "--workers", "4"], noop, cli_fetch_models())
            self.assertEqual(cli.EXIT_OK, code)
            self.assertIn("No changes required", out)
            self.assertEqual([], writes)

    def test_no_credits_skips_balance_phase_entirely(self):
        cfg = [provider("DS", [{"id": "deepseek-chat", "url": "https://api.deepseek.com"}])]

        def models(name, base_url, timeout=None, api_key=None):
            return FetchResult(base_url=base_url, provider_name=name, success=True, model_ids=["deepseek-chat"])

        def must_not_call(*a, **k):
            raise AssertionError("fetch_credits called despite --no-credits")

        code, out, *_ = self._run(["--all", "--no-credits", "--workers", "4"], cfg, models, credits=must_not_call)
        self.assertEqual(cli.EXIT_OK, code)
        self.assertNotIn("credits:", out)

    def test_workers_flag_is_forwarded_to_both_phases(self):
        cfg = [provider("DS", [{"id": "deepseek-chat", "url": "https://api.deepseek.com"}])]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cfg(tmp, cfg)
            with (
                patch("clm_sync.cli.sync_config", wraps=cli.sync_config) as sync_spy,
                patch("clm_sync.cli.enrich_with_credits") as credits_spy,
                patch(
                    "clm_sync.cli.fetch_models",
                    return_value=FetchResult("https://api.deepseek.com", "DS", True, ["deepseek-chat"]),
                ),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                cli.run(["--config", path, "--all", "--workers", "3"])
        self.assertEqual(3, sync_spy.call_args.kwargs["max_workers"])
        self.assertEqual(3, credits_spy.call_args.kwargs["workers"])

    def test_workers_default_is_automatic(self):
        self.assertIsNone(cli.build_parser().parse_args(["--config", "x", "--all"]).workers)

    def test_invalid_workers_value_is_rejected(self):
        for bad in ("0", "-2", "abc", "1.5"):
            with self.subTest(value=bad), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    cli.build_parser().parse_args(["--config", "x", "--all", "--workers", bad])
                self.assertEqual(2, ctx.exception.code)

    def test_help_mentions_workers(self):
        self.assertIn("concurrent endpoint requests", cli.build_parser().format_help())


if __name__ == "__main__":
    unittest.main()
