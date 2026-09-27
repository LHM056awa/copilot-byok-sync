"""Command-line interface for clm-sync."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

from . import __version__
from .client import fetch_credits, fetch_models
from .config import load_config, write_config_atomic
from .models import ModelSyncError, endpoint_base_urls, is_custom_endpoint
from .sync import sync_config

try:
    from .secrets import resolve_placeholder
except ImportError:
    # secrets.py only imports cleanly on Windows (it pulls in ctypes.wintypes
    # for DPAPI) and with the cryptography package installed.  On any other
    # platform, or without cryptography, placeholder resolution is a no-op:
    # literal keys still work, ${input:...} references simply degrade to a
    # keyless request (a 401).
    resolve_placeholder = None

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_CONFIG_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clm-sync",
        description=(
            "Sync the 'models' arrays of custom endpoints in VS Code's "
            "chatLanguageModels.json from OpenAI-compatible /v1/models endpoints."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  clm-sync --config chatLanguageModels.json --dry-run\n"
            "  clm-sync --config chatLanguageModels.json --provider Iris\n"
            "  clm-sync --config chatLanguageModels.json --all\n"
            "  clm-sync --config chatLanguageModels.json --all --no-delete\n"
        ),
    )
    parser.add_argument(
        "--config",
        required=True,
        help="path to chatLanguageModels.json",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--all", action="store_true", help="sync every vendor=customendpoint provider"
    )
    target.add_argument(
        "--provider",
        action="append",
        dest="providers",
        metavar="NAME",
        help="sync only the named provider (repeatable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change without writing the file",
    )
    parser.add_argument(
        "--no-delete",
        action="store_true",
        help="never remove models or their settings; only add/update",
    )
    parser.add_argument(
        "--sort",
        action="store_true",
        help="rewrite each provider's model list in ascending model-id order "
        "(lexicographic, case-sensitive); by default the existing order is "
        "preserved and new models are appended",
    )
    parser.add_argument(
        "--no-credits",
        action="store_true",
        help="skip the automatic balance lookup for known vendors (on by default)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="per-request timeout in seconds (default: 15)",
    )
    parser.add_argument("--version", action="version", version=f"clm-sync {__version__}")
    return parser


def enrich_with_credits(outcome, *, timeout, key_resolver, targets_all):
    """Attach a display-only balance value to each synced provider.

    Balances come from each provider's *known* balance endpoint (matched by
    host); providers whose base URLs are not in the known table keep
    ``credits=None`` (the report omits the line).  A failed or empty lookup is
    recorded as "unavailable".  This is display-only: it never feeds the model
    sync or the delete guard.

    *targets_all* (``--all``) pairs each ``ProviderSyncResult`` with the
    provider object it was built from via the result's ``config_index`` -- its
    position in the post-sync config -- so duplicate provider names can never
    mix up results and the pairing does not depend on list ordering.  When a
    specific subset is targeted, names are guaranteed unique by
    ``select_providers`` (a duplicate raises), so a name lookup is safe there.
    """
    from .client import has_credits_endpoint
    from .sync import resolve_provider_key

    if targets_all:
        def provider_for(result):
            idx = result.config_index
            if idx is None or not 0 <= idx < len(outcome.config):
                return None
            prov = outcome.config[idx]
            return prov if is_custom_endpoint(prov) else None
    else:
        custom = [p for p in outcome.config if is_custom_endpoint(p)]
        by_name: dict = {}
        for prov in custom:
            if isinstance(prov.get("name"), str):
                by_name.setdefault(prov["name"], prov)

        def provider_for(result):
            return by_name.get(result.name)

    for result in outcome.providers:
        prov = provider_for(result)
        if prov is None:
            continue
        urls = endpoint_base_urls(prov.get("models"))
        if not any(has_credits_endpoint(u) for u in urls):
            continue  # no known balance endpoint -> omit balance line
        key = resolve_provider_key(prov, key_resolver)
        value = None
        for url in urls:
            value = fetch_credits(result.name, url, timeout=timeout, api_key=key)
            if value is not None:
                break
        result.credits = value if value is not None else "unavailable"


# ---------------------------------------------------------------------------
# Output colouring
#
# The report is coloured with ANSI escape codes when stdout is an interactive
# terminal (e.g. a Windows Terminal / PowerShell prompt), so status lines are
# easy to scan.  It stays plain text when stdout is piped/redirected or when
# the NO_COLOR convention is honoured, so logs and CI captures remain clean.
# The machine-readable copy sent to stderr is *always* uncoloured.
# ---------------------------------------------------------------------------


def _stdout_color_enabled() -> bool:
    """Return True when we may emit ANSI colour to stdout."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


class _C:
    """ANSI colour codes (no-op placeholders when colour is disabled)."""

    def __init__(self, enabled: bool) -> None:
        if enabled:
            self.reset = "\033[0m"
            self.bold = "\033[1m"
            self.dim = "\033[2m"
            self.red = "\033[31m"
            self.green = "\033[32m"
            self.yellow = "\033[33m"
            self.cyan = "\033[36m"
        else:
            self.reset = ""
            self.bold = ""
            self.dim = ""
            self.red = ""
            self.green = ""
            self.yellow = ""
            self.cyan = ""


def _paint(c: _C, code: str, text: str) -> str:
    """Wrap *text* in a colour code (no-op when the code is empty)."""
    return f"{code}{text}{c.reset}" if code else text


def render_report(outcome, *, color: bool = False) -> str:
    c = _C(color)
    lines = []
    for provider in outcome.providers:
        status = "changes" if provider.changed else "unchanged"
        status_color = c.green if provider.changed else c.dim
        lines.append(
            f"{_paint(c, status_color, '[' + status + ']')} "
            f"{_paint(c, c.bold, provider.name)}"
        )
        if provider.added:
            lines.append(
                f"{_paint(c, c.green, '    added   ')}({len(provider.added)}): "
                + ", ".join(provider.added)
            )
        if provider.removed:
            lines.append(
                f"{_paint(c, c.red, '    removed ')}({len(provider.removed)}): "
                + ", ".join(provider.removed)
            )
        if provider.settings_keys_removed:
            lines.append(
                "    settings keys removed ({}): {}".format(
                    len(provider.settings_keys_removed),
                    ", ".join(provider.settings_keys_removed),
                )
            )
        if provider.kept:
            lines.append(
                f"{_paint(c, c.dim, '    kept    ')}({provider.kept}) "
                "with existing metadata"
            )
        if provider.credits is not None:
            lines.append(
                f"{_paint(c, c.cyan, '    credits: ')}{provider.credits}"
            )
        if provider.skipped_deletion:
            lines.append(_paint(c, c.yellow, "    deletion skipped: keeping existing models"))
        if provider.discarded_invalid:
            lines.append(
                f"{_paint(c, c.yellow, '    discarded invalid entries')} "
                "({}) — missing or non-string 'id': {}".format(
                    len(provider.discarded_invalid),
                    ", ".join(repr(e) for e in provider.discarded_invalid),
                )
            )
        if provider.no_endpoints:
            lines.append(
                _paint(
                    c,
                    c.yellow,
                    "    warning: no endpoint url — nothing to fetch, add a "
                    "'url' to at least one model",
                )
            )
        for error in provider.errors:
            lines.append(f"    {_paint(c, c.red, 'error:')} {error}")
    return "\n".join(lines)


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ModelSyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        outcome = sync_config(
            config,
            fetcher=lambda name, base_url, api_key: fetch_models(
                name, base_url, timeout=args.timeout, api_key=api_key,
            ),
            provider_names=args.providers,
            allow_delete=not args.no_delete,
            key_resolver=resolve_placeholder,
            sort_models=args.sort,
        )
    except ModelSyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    # Auto-fetch balances unless --no-credits is given.  A failure here never
    # affects model sync or the delete guard: the value is display-only.
    if not args.no_credits:
        enrich_with_credits(
            outcome,
            timeout=args.timeout,
            key_resolver=resolve_placeholder,
            targets_all=args.providers is None,
        )

    total = len(outcome.providers)
    failed = [p for p in outcome.providers if not p.ok]
    changed = [p for p in outcome.providers if p.changed]
    # Human-facing stdout is coloured on an interactive TTY; a plain copy is
    # always kept for the machine-readable stderr stream.
    stdout_color = _stdout_color_enabled()
    report = render_report(outcome, color=stdout_color) if total else ""
    plain_report = render_report(outcome) if total else ""
    _c = _C(stdout_color)

    if args.dry_run:
        print("Dry run - no files were written.")
        if report:
            print(report)
    elif outcome.changed:
        write_config_atomic(args.config, outcome.config)
        print(f"Updated {args.config}")
        if report:
            print(report)
    else:
        print(f"No changes required for {args.config}")
        if report:
            print(report)

    print(
        f"\n{_paint(_c, _c.bold, 'Summary:')} {total} provider(s) targeted, "
        f"{len(changed)} changed, {len(failed)} with errors."
    )

    # Scripts/CI read stderr; keep the human report on stderr as well, but
    # always uncoloured so it stays safe to grep / redirect.
    if failed and plain_report:
        print(plain_report, file=sys.stderr)

    return EXIT_PARTIAL_FAILURE if failed else EXIT_OK


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(run())
