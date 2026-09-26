"""Resolve VS Code BYOK API keys from encrypted Secret Storage.

`chatLanguageModels.json` placeholders like::

    ${input:chat.lm.secret.-7df7ce48}

are resolved to the real API key by reading the corresponding
`secret://chat.lm.secret.-7df7ce48` entry from the global-storage
`state.vscdb` SQLite database and decrypting its `v10` payload:

    v10 (3 B) + nonce (12 B) + ciphertext + GCM tag (16 B)

The 32-byte AES-256-GCM key is stored per-user in
`%APPDATA%\\Code\\Local State` under `os_crypt.encrypted_key`,
protected by Windows DPAPI (`CryptProtectData` / `CryptUnprotectData`).

Plaintext keys exist only in process memory, used solely to build the
`Authorization` header.  They are never printed, logged, or written to
disk.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import sqlite3
import shutil
import tempfile
from typing import Optional


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SecretResolutionError(Exception):
    """Raised when a placeholder cannot be resolved to a usable key."""


# ---------------------------------------------------------------------------
# DPAPI
# ---------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wt.DWORD),
        ("pbData", ctypes.c_void_p),
    ]


_CRYPT_UNPROTECT_DATA_FLAGS = 0x00000001  # CRYPTPROTECT_UI_FORBIDDEN
_DPAPI_PREFIX = b"DPAPI"  # 5 bytes, stripped before CryptUnprotectData


def _dpapi_unprotect(blob: bytes) -> Optional[bytes]:
    """Call CryptUnprotectData on *blob* and return the unencrypted bytes."""
    try:
        _crypt32 = ctypes.windll.crypt32
        _kernel32 = ctypes.windll.kernel32
        _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        _kernel32.LocalFree.restype = ctypes.c_void_p
        _crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wt.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        _crypt32.CryptUnprotectData.restype = wt.BOOL

        buf = ctypes.create_string_buffer(blob)
        in_blob = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.c_void_p))
        out_blob = _DATA_BLOB(0, None)

        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            _CRYPT_UNPROTECT_DATA_FLAGS,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        if out_blob.pbData:
            _kernel32.LocalFree(out_blob.pbData)
        return result
    except Exception:
        return None


# ---------------------------------------------------------------------------
# v10 payload layout
# ---------------------------------------------------------------------------
_V10_PREFIX = b"v10"
_NONCE_LEN = 12
_GCM_TAG_LEN = 16


def _decrypt_v10(payload: bytes, aes_key: bytes) -> Optional[str]:
    """Decrypt a v10 payload: nonce(12) + ciphertext + tag(16)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise SecretResolutionError(
            "cryptography package is required to decrypt VS Code secrets; "
            "install with: pip install cryptography"
        )

    if payload[:3] != _V10_PREFIX:
        return None
    if len(payload) < _NONCE_LEN + _GCM_TAG_LEN + 1:
        return None

    nonce = payload[3 : 3 + _NONCE_LEN]
    # AESGCM.decrypt(nonce, data, aad) where data = ciphertext + tag
    ct_and_tag = payload[3 + _NONCE_LEN :]
    try:
        plaintext = AESGCM(aes_key).decrypt(nonce, ct_and_tag, None)
        return plaintext.decode("utf-8")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _global_storage_db_path() -> str:
    """Return the path to VS Code stable's globalStorage state.vscdb."""
    return os.path.join(
        os.environ.get("APPDATA", ""),
        "Code",
        "User",
        "globalStorage",
        "state.vscdb",
    )


def _local_state_path() -> str:
    return os.path.join(os.environ.get("APPDATA", ""), "Code", "Local State")


def _load_master_key() -> Optional[bytes]:
    """Return the 32-byte AES-256-GCM master key from Local State.

    Any problem reading the file (missing, malformed JSON, unexpected
    shape, DPAPI failure) yields None - a broken Local State must not
    crash the sync run, it only means "no key available".
    """
    ls_path = _local_state_path()
    if not os.path.isfile(ls_path):
        return None

    try:
        with open(ls_path, encoding="utf-8") as fh:
            ls = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None

    if not isinstance(ls, dict):
        return None

    os_crypt = ls.get("os_crypt")
    if not isinstance(os_crypt, dict):
        return None
    enc_key_b64 = os_crypt.get("encrypted_key", "")
    if not isinstance(enc_key_b64, str) or not enc_key_b64:
        return None

    try:
        blob = base64.b64decode(enc_key_b64)
    except Exception:
        return None
    if not blob.startswith(_DPAPI_PREFIX):
        return None

    # Strip 5-byte DPAPI prefix, then unprotect
    key = _dpapi_unprotect(blob[len(_DPAPI_PREFIX) :])
    if key is None or len(key) != 32:
        return None
    return key


def resolve_placeholder(placeholder: str) -> Optional[str]:
    """Resolve a ``${input:chat.lm.secret.<id>}`` placeholder to the real key.

    Returns ``None`` when the placeholder is not recognised or the key
    cannot be resolved, for *any* reason (missing cryptography package,
    damaged Local State, database lock, ...).  Resolution must never take
    down the whole sync run: an unresolvable reference degrades to a
    keyless request, exactly like a missing key.
    """
    prefix = "${input:"
    suffix = "}"
    if not (placeholder.startswith(prefix) and placeholder.endswith(suffix)):
        return None

    inner = placeholder[len(prefix) : -len(suffix)]
    # inner looks like: chat.lm.secret.-7df7ce48
    db_key = "secret://" + inner

    try:
        return _read_secret(db_key)
    except Exception:
        # Last line of defence: no resolution failure may propagate.
        return None


def _read_secret(db_key: str) -> Optional[str]:
    """Read and decrypt a single secret from globalStorage state.vscdb.

    The original database may be held open (WAL/shared) by a running VS
    Code instance, so we copy it first.  A running instance keeps un-checkpointed
    pages in the `state.vscdb-wal` sidecar (and metadata in `state.vscdb-shm`);
    copying only the main file would give an inconsistent snapshot that is
    stale or, under lock contention, unreadable - both degrade to "no key"
    (a 401) rather than the real data.  We therefore copy the main file plus
    any WAL/SHM sidecars: SQLite applies the WAL to the copy automatically on
    open, yielding a consistent read.  Cleanup of the copies is best-effort:
    the decrypted value is captured into memory before any file is released,
    and a lingering SQLite handle on Windows must never break resolution.
    """
    db_path = _global_storage_db_path()
    if not os.path.isfile(db_path):
        return None

    aes_key = _load_master_key()
    if aes_key is None:
        return None

    raw_value: Optional[str] = None
    with tempfile.TemporaryDirectory(prefix=".clm-secret-") as tmp_dir:
        tmp_path = os.path.join(tmp_dir, "state.vscdb")
        try:
            shutil.copyfile(db_path, tmp_path)
            # Copy the WAL/SHM sidecars if they exist, so the main-file copy
            # applies them on open and reflects the writer's committed state.
            for sidecar in ("-wal", "-shm"):
                src_sidecar = db_path + sidecar
                if os.path.isfile(src_sidecar):
                    try:
                        shutil.copyfile(
                            src_sidecar, tmp_path + sidecar
                        )
                    except OSError:
                        # A sidecar that vanished mid-copy is harmless: we fall
                        # back to the main file only.
                        pass
            con = sqlite3.connect(tmp_path)
            try:
                row = con.execute(
                    "SELECT value FROM ItemTable WHERE key = ?", (db_key,)
                ).fetchone()
            finally:
                con.close()
            if row:
                raw_value = row[0]
        except sqlite3.Error:
            return None
        except OSError:
            # Copying the database can fail (in use, permissions, disk);
            # that is a "no key" situation, not a crash.
            return None

    if raw_value is None:
        return None
    if isinstance(raw_value, bytes):
        raw_value = raw_value.decode("utf-8", errors="replace")
    try:
        envelope = json.loads(raw_value)
    except json.JSONDecodeError:
        return None

    data = envelope.get("data")
    if data is None:
        return None

    # data is a list of byte values (0-255) in the SQLite JSON encoding
    if isinstance(data, list):
        payload = bytes(data)
        return _decrypt_v10(payload, aes_key)

    # Fallback: data might be a base64 string
    if isinstance(data, str):
        try:
            payload = base64.b64decode(data)
            return _decrypt_v10(payload, aes_key)
        except Exception:
            return None

    return None
