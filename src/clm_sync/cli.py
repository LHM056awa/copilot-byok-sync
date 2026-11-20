"""Command-line interface for clm-sync."""

from __future__ import annotations

import argparse
import ctypes
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

# "Grey" has to be spelled differently per terminal, because the two Windows
# console hosts disagree about SGR 2 (faint):
#
#   * Windows Terminal, the VS Code integrated terminal (xterm.js) and Unix
#     terminals *implement* faint and render it as a light grey, so SGR 2 is
#     used there -- that spelling is what keeps this report byte-identical to
#     the sibling tool dsh-byok-sync.
#   * A legacy conhost (the window you get when double-clicking the .bat, i.e.
#     cmd.exe's own console) *ignores* SGR 2.  Measured on this machine by
#     writing the escape and reading the screen-buffer cell attributes back:
#     faint leaves 0x00 (black on black, so ``[unchanged]`` disappears) and
#     never a grey.  Bright black (SGR 90) *is* honoured there and lands on
#     0x08, which reads as grey.  So conhost gets faint + bright black.
_MUTED_FAINT = "\033[2m"
_MUTED_CONHOST = "\033[2;90m"

#: Environment markers of terminals known to implement SGR 2 (faint).  Absent
#: all of them, the conservative conhost spelling is used: bright black renders
#: grey everywhere, while a dropped faint would not render at all.
_FAINT_MARKERS = (
    "WT_SESSION",  # Windows Terminal
    "TERM_PROGRAM",  # VS Code / iTerm / Apple Terminal / vscode-server
    "COLORTERM",
    "ANSICON",
    "ConEmuANSI",
    "TERM",  # xterm-compatible ptys
)


def _grey_code(environ=None) -> str:
    """Return the escape that actually renders grey on *this* terminal.

    Faint-capable terminals get the reference project's ``\\033[2m``; a legacy
    conhost, which drops faint on the floor, gets ``\\033[2;90m`` (faint +
    bright black) so ``[unchanged]`` / ``kept`` still read as grey instead of
    vanishing into the body text.

    *environ* is a test seam; production reads :data:`os.environ`.
    """
    env = os.environ if environ is None else environ
    if any(env.get(marker) for marker in _FAINT_MARKERS):
        return _MUTED_FAINT
    return _MUTED_CONHOST


def _windows_vt100_ready() -> bool:
    """Best-effort check that stdout's console can render ANSI (VT100) codes.

    Windows-only; every other platform returns True, leaving the pre-existing
    (Linux/macOS/WSL) behaviour completely untouched.

    On Windows:
      * A *real* conhost console is probed with GetConsoleMode:
          - the ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) bit is already on
            -> True;
          - it is off -> we call SetConsoleMode to turn it on; True on
            success, False on failure (a very old OS that cannot do VT).
            This is what lets plain ``cmd.exe`` / PowerShell render colour
            instead of dumping raw escape codes.
      * A *non-real* console handle (GetConsoleMode fails) is an
        ANSI-emulating pty -- e.g. the VS Code integrated terminal or xterm --
        which already renders ANSI, so we return True to keep its current
        coloured behaviour.
      * Any ctypes problem degrades to False (no colour, never crashes the
        run), mirroring the defensive style used by ``secrets.py``.
    """
    if os.name != "nt":
        return True
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    try:
        # ctypes.wintypes is a Windows-only module; import it lazily so the
        # top-level module import stays portable to Linux/macOS.
        import ctypes.wintypes as wt
    except Exception:
        return False
    try:
        kernel32 = windll.kernel32
        kernel32.GetStdHandle.argtypes = [wt.DWORD]
        kernel32.GetStdHandle.restype = wt.HANDLE
        kernel32.GetConsoleMode.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        kernel32.GetConsoleMode.restype = wt.BOOL
        kernel32.SetConsoleMode.argtypes = [wt.HANDLE, wt.DWORD]
        kernel32.SetConsoleMode.restype = wt.BOOL
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wt.DWORD(0)
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # Not a real conhost handle -> an emulated terminal that renders
            # ANSI natively (VS Code integrated terminal, xterm, ...).
            return True
        _ENABLE_VT = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if mode.value & _ENABLE_VT:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VT))
    except Exception:
        return False


def _stdout_color_enabled() -> bool:
    """Return True when we may emit ANSI colour to stdout.

    Colour is allowed only when stdout is an interactive terminal *and* that
    terminal can actually render ANSI: on Windows a real console must have
    virtual-terminal (VT100) processing available (enabled here if possible),
    and an emulated / ANSI-native terminal (e.g. the VS Code integrated
    terminal) is trusted to render it.  Piped or redirected stdout, and the
    NO_COLOR convention, stay plain text.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        if not sys.stdout.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.name == "nt":
        return _windows_vt100_ready()
    return True


class _C:
    """ANSI colour codes (no-op placeholders when colour is disabled).

    ``dim`` -- the grey used for ``[unchanged]`` and the ``kept`` label -- is
    resolved per run by :func:`_grey_code`, because conhost needs a different
    spelling than Windows Terminal (see above).  *grey* is a test seam.
    """

    def __init__(self, enabled: bool, grey: Optional[str] = None) -> None:
        if enabled:
            self.reset = "\033[0m"
            self.bold = "\033[1m"
            self.dim = _grey_code() if grey is None else grey
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
        if provider.cross_settings_removed:
            lines.append(
                f"{_paint(c, c.yellow, '    cross-provider settings removed')} "
                "({}): {}".format(
                    len(provider.cross_settings_removed),
                    ", ".join(provider.cross_settings_removed),
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
        if provider.filtered_non_text:
            lines.append(
                f"{_paint(c, c.yellow, '    filtered non-text')} "
                "({}) — id/name matched video/image/audio/...: {}".format(
                    len(provider.filtered_non_text),
                    ", ".join(provider.filtered_non_text),
                )
            )
        if provider.no_signal_endpoints:
            lines.append(
                f"{_paint(c, c.yellow, '    no text signal')} "
                "({}) — endpoint(s) returned no text model, their local "
                "models were kept: {}".format(
                    len(provider.no_signal_endpoints),
                    ", ".join(provider.no_signal_endpoints),
                )
            )
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

    # Scripts/CI read stderr.  Mirror the full report there only when stdout
    # is *not* an interactive terminal -- i.e. the user cannot already see it
    # on stdout -- and always uncoloured so it stays safe to grep / redirect.
    # This is orthogonal to `failed`: a zero-failure CI run still gets the
    # report, while an interactive TTY run is never duplicated.
    try:
        stdout_is_tty = sys.stdout.isatty()
    except (AttributeError, ValueError):
        # No usable isatty() -> treat as non-interactive and mirror, which is
        # the safe default for the scripted / CI consumers of stderr.
        stdout_is_tty = False
    if plain_report and not stdout_is_tty:
        print(plain_report, file=sys.stderr)

    return EXIT_PARTIAL_FAILURE if failed else EXIT_OK


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(run())
