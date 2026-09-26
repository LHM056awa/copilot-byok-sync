"""HTTP transport for OpenAI-compatible /v1/models endpoints.

The transport is injectable so tests can run fully offline.
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

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
    request.add_header("User-Agent", "clm-sync/0.1.0")
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
