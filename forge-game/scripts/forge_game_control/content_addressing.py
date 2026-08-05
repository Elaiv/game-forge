from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

import rfc8785

from .errors import InvalidJsonError


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError) as exc:
        raise InvalidJsonError(f"Value cannot be canonicalized as RFC 8785 JSON: {exc}") from exc


def content_hash(value: Any) -> str:
    digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return f"sha256:{digest}"


def envelope_content_hash(document: dict[str, Any]) -> str:
    payload = deepcopy(document)
    payload.pop("content_hash", None)
    return content_hash(payload)
