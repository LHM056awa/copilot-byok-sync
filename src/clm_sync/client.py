"""HTTP transport for OpenAI-compatible /v1/models endpoints.

The transport is injectable so tests can run fully offline.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .models import FetchResult, normalize_id

LOGGER = logging.getLogger(__name__)

Transport = Callable[[str, Optional[float], Optional[str]], str]

# Public default; kept modest so one dead endpoint cannot stall a whole run.
DEFAULT_TIMEOUT = 15.0


class TransportError(Exception):
    """Raised by a transport when a request cannot return a usable body."""


class ModelsTransport(Protocol):
    def __call__(
        self,
        url: str,
        timeout: Optional[float],
        api_key: Optional[str] = None,
    ) -> str: ...


def resolve_models_url(base_url: str) -> str:
    """Map a provider base url onto its OpenAI-compatible /v1/models endpoint.

    The URL you provide to the sync tool may be:
      - `https://api.example.com` (recommended - clean base URL)
      - `https://api.example.com/v1`
      - `https://api.example.com/v1/chat/completions` (old style, still supported)
      - `https://api.example.com/v1/models`
      - `https://api.example.com/v1/responses` (Responses-API gateways)
      - any of the above with a trailing slash, uppercase path, or query string

    Query/fragment are stripped (the models list is not parameterized),
    the path is normalised case-sensitively only for the known chat-completions
    and responses sub-paths, and the result is always `.../v1/models`.
    """
    cleaned = base_url.strip()
    if not cleaned:
        raise ValueError("base URL must not be empty")

    # Drop query/fragment before normalising the path.
    parts = urlsplit(cleaned)
    path = parts.path

    # Strip the known OpenAI chat-completions / responses sub-paths (case-insensitive).
    stripped_len = 0
    for suffix in ("/v1/chat/completions", "/v1/responses"):
        if path.lower().rstrip("/").endswith(suffix):
            stripped_len = len(suffix)
            break
    if stripped_len:
        path = path[: len(path.rstrip("/")) - stripped_len] if path.rstrip("/") else ""

    path = path.rstrip("/")
    lowered = path.lower()

    if lowered.endswith("/v1/models") or lowered.endswith("/models"):
        result_path = path
    elif lowered.endswith("/v1"):
        result_path = path + "/models"
    elif lowered in ("", "/"):
        result_path = "/v1/models"
    else:
        result_path = path + "/v1/models"

    return urlunsplit(
        (parts.scheme, parts.netloc, result_path, "", "")
    )


def _parse_model_entries(body: str) -> tuple[list[str], Dict[str, Dict[str, Any]]]:
    """Extract model ids and their metadata from a /v1/models body.

    Accepts the OpenAI shape {"data": [...]}, a bare list of objects or
    strings, a single {"id": ...} object (wrapped as a one-element list), or
    {"data": {"id": ...}}.  Any other shape for the entry collection is a
    transport-level failure: iterating a dict's *keys* or a string's
    *characters* would fabricate garbage model ids, which under default
    delete rules would destroy the local model list.
    """
    try:
        payload: Any = json.loads(body)
    except json.JSONDecodeError as exc:
        raise TransportError(f"response is not valid JSON ({exc})") from exc

    if isinstance(payload, dict):
        raw_items: Any = payload.get("data")
        if raw_items is None:
            raise TransportError("no usable model ids found in response")
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raise TransportError("unexpected JSON payload type; expected object or list")

    # Normalise to a list of entries before iterating.
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raise TransportError("'data' is not a list of model entries")

    seen: Dict[str, None] = {}
    metadata: Dict[str, Dict[str, Any]] = {}
    for item in raw_items:
        raw_id: Any = item if isinstance(item, str) else None
        if isinstance(item, dict):
            raw_id = item.get("id", item.get("name"))
        model_id = normalize_id(raw_id)
        if model_id is not None:
            seen.setdefault(model_id, None)
            if isinstance(item, dict):
                metadata.setdefault(model_id, dict(item))

    if not seen:
        raise TransportError("no usable model ids found in response")
    return list(seen.keys()), metadata


def parse_models_payload(body: str) -> list[str]:
    """Extract de-duplicated, order-preserving model ids from a /v1/models body."""
    model_ids, _ = _parse_model_entries(body)
    return model_ids


def default_transport(
    url: str,
    timeout: Optional[float] = DEFAULT_TIMEOUT,
    api_key: Optional[str] = None,
) -> str:
    """GET url and return the decoded body, raising TransportError on any failure.

    When *api_key* is given, an ``Authorization: Bearer <key>`` header is
    attached.  The key is used only in the outbound request and is never
    printed or logged.
    """
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", f"clm-sync/{__version__}")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            raw = response.read()
        return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not api_key:
            raise TransportError("HTTP 401 (no API key supplied)") from exc
        raise TransportError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise TransportError("request timed out") from exc
        if isinstance(reason, ssl.SSLError):
            raise TransportError(f"TLS error: {reason}") from exc
        raise TransportError(str(reason) if reason else "network error") from exc
    except TimeoutError as exc:
        raise TransportError("request timed out") from exc
    except OSError as exc:  # socket errors surface here too
        raise TransportError(str(exc) or "network error") from exc


def fetch_models(
    provider_name: str,
    base_url: str,
    timeout: Optional[float] = DEFAULT_TIMEOUT,
    transport: Optional[Transport] = None,
    api_key: Optional[str] = None,
) -> FetchResult:
    """Fetch model ids for one base_url, converting failures into FetchResult.

    *api_key* (when given) is attached to the request as a
    ``Authorization: Bearer`` header.  It is never logged.
    """
    try:
        url = resolve_models_url(base_url)
    except ValueError as exc:
        return FetchResult(
            base_url=base_url,
            provider_name=provider_name,
            success=False,
            error=str(exc),
        )

    call = transport or default_transport
    try:
        body = call(url, timeout, api_key)
        model_ids, model_metadata = _parse_model_entries(body)
    except TransportError as exc:
        LOGGER.debug("%s: %s failed: %s", provider_name, url, exc)
        return FetchResult(
            base_url=base_url,
            provider_name=provider_name,
            success=False,
            error=str(exc),
        )
    except Exception as exc:  # defensive: never let one endpoint abort the run
        LOGGER.debug("%s: %s unexpected failure: %s", provider_name, url, exc)
        return FetchResult(
            base_url=base_url,
            provider_name=provider_name,
            success=False,
            error=f"unexpected error: {exc}",
        )

    return FetchResult(
        base_url=base_url,
        provider_name=provider_name,
        success=True,
        model_ids=model_ids,
        model_metadata=model_metadata,
    )

def _json_path(payload: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted JSON path such as ``balance_infos[0].total_balance``.

    Each segment is a key optionally followed by integer ``[n]`` index parts.
    An index part is accepted only when its content is a plain ASCII decimal
    non-negative integer (``[0]``, ``[12]``).  Everything else is a malformed
    spec path and is rejected outright rather than silently ignored, so a
    mistyped path cannot resolve to the wrong value.  Concretely, rejected
    bracket contents include: empty (``[]``), non-numeric (``[abc]``, ``[²]``),
    signed (``[-1]``), and non-integer (``[1.5]``).  Unicode digits such as
    ``"²"`` are rejected even though ``str.isdigit`` would accept them, because
    ``int("²")`` raises ``ValueError`` -- the check is therefore an explicit
    ASCII ``[0-9]+`` match, not ``str.isdigit``.

    Returns ``(found, value)`` so callers can tell "key missing" apart from a
    legitimate ``0`` / ``False`` value.
    """
    cur = payload
    for seg in path.split("."):
        brackets = re.findall(r"\[([^\]]*)\]", seg)
        if any(re.fullmatch(r"[0-9]+", b) is None for b in brackets):
            return False, None
        key = re.sub(r"\[[^\]]*\]", "", seg).strip()
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
        for b in brackets:
            i = int(b)
            if not isinstance(cur, list) or not 0 <= i < len(cur):
                return False, None
            cur = cur[i]
    return True, cur


# Balance endpoints for the vendors whose balance we know how to read, ported
# from MeteorNOX/DeepSeek-Balance-Whale-Widget's API_TEMPLATES.  Keyed by the
# host (netloc) of the provider's base URL.  Only these *known* endpoints are
# ever queried; any other host gets no balance lookup at all (its balance line
# is simply omitted from the report rather than shown as "unavailable").
#
# Each spec carries: the absolute balance URL, a list of
# ``(json_path, label, scale)`` fields to extract, and an optional currency.
_CREDITS_ENDPOINTS: Dict[str, Dict[str, Any]] = {
    "api.deepseek.com": {
        "url": "https://api.deepseek.com/user/balance",
        "fields": [("balance_infos[0].total_balance", "balance", 1.0)],
        "currency": "CNY",
    },
    "openrouter.ai": {
        "url": "https://openrouter.ai/api/v1/credits",
        "fields": [
            ("data.total_credits", "credits", 1.0),
            ("data.total_usage", "used", 0.01),  # reported in cents
        ],
        "currency": "USD",
    },
    "api.moonshot.cn": {
        "url": "https://api.moonshot.cn/v1/users/me/balance",
        "fields": [("data.available_balance", "available", 1.0)],
        "currency": "CNY",
    },
    "api.moonshot.ai": {
        "url": "https://api.moonshot.ai/v1/users/me/balance",
        "fields": [("data.available_balance", "available", 1.0)],
        "currency": "USD",
    },
    "api.stepfun.com": {
        "url": "https://api.stepfun.com/v1/accounts",
        "fields": [("balance", "balance", 1.0)],
        "currency": "CNY",
    },
    "api.novita.ai": {
        "url": "https://api.novita.ai/v3/user/balance",
        "fields": [("availableBalance", "available", 0.0001)],
        "currency": "USD",
    },
}


def _credits_spec_for(base_url: str) -> Optional[Dict[str, Any]]:
    """Return the balance-endpoint spec for *base_url*'s host, or ``None``."""
    cleaned = (base_url or "").strip()
    if not cleaned:
        return None
    netloc = urlsplit(cleaned).netloc.lower()
    return _CREDITS_ENDPOINTS.get(netloc)


def has_credits_endpoint(base_url: str) -> bool:
    """True when *base_url* belongs to a host with a known balance endpoint.

    Only these hosts are worth querying; the report omits the balance line
    for everything else instead of guessing a path that does not exist.
    """
    return _credits_spec_for(base_url) is not None


def parse_credits_payload(body: str, spec: Dict[str, Any]) -> Optional[str]:
    """Render a known-endpoint balance body into a short display string.

    Extracts each ``(json_path, label, scale)`` field declared in *spec* and
    formats it (appending the currency).  Returns ``None`` when the body is
    empty or not valid JSON (an HTML error page or gateway response carries no
    readable balance, so echoing it would mislead).  When the JSON parses but
    none of the declared fields is present — e.g. the vendor reshaped its
    response — a truncated raw body is returned so the operator can see what
    changed.  A single field is returned bare; multiple fields are labelled.
    """
    text = (body or "").strip()
    if not text:
        return None

    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        # Non-JSON body (HTML error page, gateway message): nothing readable.
        return None

    parts: list[tuple[str, str]] = []
    fields = spec.get("fields", [])
    if isinstance(payload, (dict, list)):
        for json_path, label, scale in fields:
            found, value = _json_path(payload, json_path)
            if not found or value is None:
                continue
            # Some vendors report the number as a string (e.g. "1.23"); coerce
            # so the scale factor still applies instead of being skipped.
            if isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    pass
            if isinstance(value, (int, float)) and scale != 1.0:
                value = value * scale
            rendered = str(value)
            if spec.get("currency"):
                rendered += f" {spec['currency']}"
            parts.append((label, rendered))

    if parts:
        # A spec that declares a single field reports its value bare (e.g.
        # "1.23 CNY").  A multi-field spec always labels every surviving value
        # (e.g. "used: 5 USD"), so a missing or reshaped field can never be
        # mis-read as a different metric.
        if len(fields) == 1:
            return parts[0][1]
        return ", ".join(f"{label}: {val}" for label, val in parts)

    return text[:120] + ("..." if len(text) > 120 else "")


def fetch_credits(
    provider_name: str,
    base_url: str,
    timeout: Optional[float] = DEFAULT_TIMEOUT,
    transport: Optional[Transport] = None,
    api_key: Optional[str] = None,
) -> Optional[str]:
    """Fetch a provider's balance from its *known* balance endpoint.

    Only hosts present in :data:`_CREDITS_ENDPOINTS` are queried; any other
    host returns ``None`` without a network call (the report simply omits the
    balance line).  For a known host, a request or parse failure also yields
    ``None`` so a missing/failed balance endpoint never disturbs model
    syncing.  Returns a short, human-readable value.
    """
    spec = _credits_spec_for(base_url)
    if spec is None:
        return None

    url = spec["url"]
    call = transport or default_transport
    try:
        body = call(url, timeout, api_key)
    except TransportError as exc:
        LOGGER.debug("%s: credits %s failed: %s", provider_name, url, exc)
        return None
    except Exception as exc:  # defensive: never let one endpoint abort the run
        LOGGER.debug("%s: credits %s unexpected: %s", provider_name, url, exc)
        return None

    return parse_credits_payload(body, spec)
