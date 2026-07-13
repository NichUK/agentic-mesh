from __future__ import annotations

import hashlib
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any


PERSISTENCE_POLICY_ID = "nul-sha256-marker-v1"
BINARY_OMISSIONS_KEY = "_agentic_mesh_binary_omissions"


class BinaryPersistenceRejected(ValueError):
    """Raised when an intentional durable input contains binary data."""


@dataclass(frozen=True)
class BinaryOmission:
    path: str
    kind: str
    sha256: str
    byte_length: int
    nul_count: int


def sanitize_persisted_text(
    value: str, *, path: str
) -> tuple[str, tuple[BinaryOmission, ...]]:
    """Replace NUL-bearing text with an explicit, non-reversible hash marker.

    PostgreSQL rejects U+0000 in text values. Removing or substituting only the
    offending character would silently change evidence, so the complete value
    is omitted and represented by its UTF-8 byte length, NUL count, and SHA-256.
    Raw binary evidence belongs in a file/object store; database rows retain the
    marker and hash only.
    """

    if "\x00" not in value:
        return value, ()
    raw = value.encode("utf-8", errors="surrogatepass")
    omission = BinaryOmission(
        path=path,
        kind="nul_text",
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        nul_count=raw.count(b"\x00"),
    )
    return _omission_marker(omission), (omission,)


def sanitize_json_payload(payload: dict[str, Any], *, path: str) -> dict[str, Any]:
    """Return a JSON-safe payload with explicit metadata for omitted binary values."""

    sanitized, omissions = _sanitize_json_value(payload, path=path)
    assert isinstance(sanitized, dict)
    if not omissions:
        return sanitized
    metadata_key = _available_metadata_key(sanitized)
    sanitized[metadata_key] = {
        "policy": PERSISTENCE_POLICY_ID,
        "raw_storage": "external_file_or_hash_only",
        "omissions": [asdict(item) for item in omissions],
    }
    return sanitized


def reject_binary_values(value: Any, *, path: str) -> None:
    """Reject binary values at semantic-write boundaries without echoing them."""

    omission = _first_binary_value(value, path=path)
    if omission is None:
        return
    raise BinaryPersistenceRejected(
        "binary durable input rejected; "
        f"policy={PERSISTENCE_POLICY_ID}; path={omission.path}; kind={omission.kind}; "
        f"sha256={omission.sha256}; bytes={omission.byte_length}; "
        f"nul_count={omission.nul_count}"
    )


def _sanitize_json_value(
    value: Any, *, path: str
) -> tuple[Any, tuple[BinaryOmission, ...]]:
    if isinstance(value, str):
        return sanitize_persisted_text(value, path=path)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        omission = BinaryOmission(
            path=path,
            kind="binary_bytes",
            sha256=hashlib.sha256(raw).hexdigest(),
            byte_length=len(raw),
            nul_count=raw.count(b"\x00"),
        )
        return _omission_marker(omission), (omission,)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        omissions: list[BinaryOmission] = []
        for index, (key, item) in enumerate(value.items()):
            key_text = key if isinstance(key, str) else str(key)
            sanitized_key, key_omissions = sanitize_persisted_text(
                key_text,
                path=f"{path}.<key:{index}>",
            )
            if sanitized_key in sanitized:
                sanitized_key = f"{sanitized_key} [collision:{index}]"
            sanitized_item, item_omissions = _sanitize_json_value(
                item,
                path=f"{path}.{sanitized_key}",
            )
            sanitized[sanitized_key] = sanitized_item
            omissions.extend(key_omissions)
            omissions.extend(item_omissions)
        return sanitized, tuple(omissions)
    if isinstance(value, (list, tuple)):
        sanitized_items: list[Any] = []
        omissions: list[BinaryOmission] = []
        for index, item in enumerate(value):
            sanitized_item, item_omissions = _sanitize_json_value(
                item, path=f"{path}[{index}]"
            )
            sanitized_items.append(sanitized_item)
            omissions.extend(item_omissions)
        return sanitized_items, tuple(omissions)
    return value, ()


def _first_binary_value(value: Any, *, path: str) -> BinaryOmission | None:
    if isinstance(value, str):
        if "\x00" not in value:
            return None
        return _binary_omission(
            value.encode("utf-8", errors="surrogatepass"), path=path, kind="nul_text"
        )
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _binary_omission(bytes(value), path=path, kind="binary_bytes")
    if isinstance(value, dict):
        for index, (key, item) in enumerate(value.items()):
            key_omission = _first_binary_value(key, path=f"{path}.<key:{index}>")
            if key_omission is not None:
                return key_omission
            item_omission = _first_binary_value(item, path=f"{path}.{key}")
            if item_omission is not None:
                return item_omission
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            omission = _first_binary_value(item, path=f"{path}[{index}]")
            if omission is not None:
                return omission
    return None


def _binary_omission(raw: bytes, *, path: str, kind: str) -> BinaryOmission:
    return BinaryOmission(
        path=path,
        kind=kind,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        nul_count=raw.count(b"\x00"),
    )


def _omission_marker(omission: BinaryOmission) -> str:
    return (
        "[agentic-mesh binary value omitted; "
        f"policy={PERSISTENCE_POLICY_ID}; kind={omission.kind}; "
        f"sha256={omission.sha256}; bytes={omission.byte_length}; "
        f"nul_count={omission.nul_count}]"
    )


def _available_metadata_key(payload: dict[str, Any]) -> str:
    if BINARY_OMISSIONS_KEY not in payload:
        return BINARY_OMISSIONS_KEY
    index = 1
    while f"{BINARY_OMISSIONS_KEY}_{index}" in payload:
        index += 1
    return f"{BINARY_OMISSIONS_KEY}_{index}"
