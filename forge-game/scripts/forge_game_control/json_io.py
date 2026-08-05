from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import DuplicateKeyError, InvalidJsonError


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"Duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise InvalidJsonError(f"Non-finite JSON number is forbidden: {value}")


def loads_json(text: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except DuplicateKeyError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise InvalidJsonError(str(exc)) from exc


def load_json(path: str | Path) -> Any:
    source = Path(path)
    try:
        return loads_json(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InvalidJsonError(f"Cannot read JSON document {source}: {exc}") from exc


def dumps_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
