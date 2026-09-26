"""Command-line interface for clm-sync."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from . import __version__
from .client import fetch_models
from .config import load_config, write_config_atomic
from .models import ModelSyncError, is_custom_endpoint
from .sync import sync_config

try:
    from .secrets import resolve_placeholder
except ImportError:  # cryptography not installed; placeholders become no-ops
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
        "--timeout",
        type=float,
        default=15.0,
        help="per-request timeout in seconds (default: 15)",
    )
    parser.add_argument("--version", action="version", version=f"clm-sync {__version__}")
    return parser


def render_report(outcome) -> str:
    lines = []
    for provider in outcome.providers:
        status = "changes" if provider.changed else "unchanged"
        lines.append(f"[{status}] {provider.name}")
        if provider.added:
            lines.append(f"    added   ({len(provider.added)}): " + ", ".join(provider.added))
        if provider.removed:
            lines.append(f"    removed ({len(provider.removed)}): " + ", ".join(provider.removed))
        if provider.settings_keys_removed:
            lines.append(
                "    settings keys removed ({}): {}".format(
                    len(provider.settings_keys_removed),
                    ", ".join(provider.settings_keys_removed),
                )
            )
        if provider.kept:
            lines.append(f"    kept    ({provider.kept}) with existing metadata")
        if provider.skipped_deletion:
            lines.append("    deletion skipped: keeping existing models")
        if provider.discarded_invalid:
            lines.append(
                "    discarded invalid entries ({}) — missing or non-string 'id': {}".format(
                    len(provider.discarded_invalid),
                    ", ".join(repr(e) for e in provider.discarded_invalid),
                )
            )
        for error in provider.errors:
            lines.append(f"    error: {error}")
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

    total = len(outcome.providers)
    failed = [p for p in outcome.providers if not p.ok]
    changed = [p for p in outcome.providers if p.changed]
    report = render_report(outcome) if total else ""

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
        f"\nSummary: {total} provider(s) targeted, "
        f"{len(changed)} changed, {len(failed)} with errors."
    )

    # Scripts/CI read stderr; keep the human report on stdout as well.
    if failed and report:
        print(report, file=sys.stderr)

    return EXIT_PARTIAL_FAILURE if failed else EXIT_OK


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(run())
