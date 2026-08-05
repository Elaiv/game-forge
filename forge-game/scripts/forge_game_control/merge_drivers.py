from __future__ import annotations

import configparser
import datetime as dt
import difflib
import io
import json
import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

from .errors import MergeDriverError
from .json_io import dumps_pretty, loads_json


_MISSING = object()
_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class MergeResult:
    content: bytes | None
    conflict: bool
    reason: str


MergeFunction = Callable[[bytes, bytes, bytes], MergeResult]


class MergeDriverRegistry:
    def __init__(self):
        self._drivers: dict[str, MergeFunction] = {
            "binary": _binary_merge,
            "git-attributes": _git_attributes_merge,
            "git-ignore": _text_merge,
            "ini": _ini_merge,
            "json": _json_merge,
            "text": _text_merge,
            "toml": _toml_merge,
        }

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._drivers))

    def select(self, target_path: str, renderer: str | None, *payloads: bytes) -> str:
        name = PurePosixPath(target_path).name
        suffix = PurePosixPath(target_path).suffix.lower()
        if name == ".gitattributes":
            return "git-attributes"
        if name == ".gitignore":
            return "git-ignore"
        if renderer in {"json"} or suffix == ".json":
            return "json"
        if renderer == "toml" or suffix == ".toml":
            return "toml"
        if renderer == "ini" or suffix in {".ini", ".cfg"}:
            return "ini"
        if all(_is_text(item) for item in payloads):
            return "text"
        return "binary"

    def merge(self, driver_id: str, base: bytes, current: bytes, desired: bytes) -> MergeResult:
        driver = self._drivers.get(driver_id)
        if driver is None:
            return MergeResult(None, True, "merge_driver_missing")
        try:
            return driver(base, current, desired)
        except Exception as exc:
            return MergeResult(None, True, f"merge_driver_failed:{type(exc).__name__}")


def _json_merge(base: bytes, current: bytes, desired: bytes) -> MergeResult:
    try:
        values = [loads_json(item.decode("utf-8")) for item in (base, current, desired)]
    except (UnicodeDecodeError, ValueError) as exc:
        return MergeResult(None, True, f"invalid_json:{type(exc).__name__}")
    merged, conflict = _merge_value(*values)
    if conflict:
        return MergeResult(None, True, "overlapping_structured_change")
    return MergeResult(dumps_pretty(merged).encode("utf-8"), False, "merged_json")


def _toml_merge(base: bytes, current: bytes, desired: bytes) -> MergeResult:
    try:
        values = [tomllib.loads(item.decode("utf-8")) for item in (base, current, desired)]
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return MergeResult(None, True, f"invalid_toml:{type(exc).__name__}")
    merged, conflict = _merge_value(*values)
    if conflict:
        return MergeResult(None, True, "overlapping_structured_change")
    try:
        payload = _toml_dumps(merged).encode("utf-8")
    except MergeDriverError as exc:
        return MergeResult(None, True, f"toml_serialization_failed:{exc}")
    return MergeResult(payload, False, "merged_toml")


def _ini_merge(base: bytes, current: bytes, desired: bytes) -> MergeResult:
    try:
        values = [_ini_load(item) for item in (base, current, desired)]
    except (UnicodeDecodeError, configparser.Error) as exc:
        return MergeResult(None, True, f"invalid_ini:{type(exc).__name__}")
    merged, conflict = _merge_value(*values)
    if conflict:
        return MergeResult(None, True, "overlapping_structured_change")
    return MergeResult(_ini_dump(merged), False, "merged_ini")


def _git_attributes_merge(base: bytes, current: bytes, desired: bytes) -> MergeResult:
    try:
        values = [_attributes_load(item) for item in (base, current, desired)]
    except (UnicodeDecodeError, MergeDriverError) as exc:
        return MergeResult(None, True, f"invalid_gitattributes:{type(exc).__name__}")
    merged, conflict = _merge_value(*values)
    if conflict:
        return MergeResult(None, True, "overlapping_attribute_change")
    lines = [f"{pattern} {attributes}".rstrip() for pattern, attributes in sorted(merged.items())]
    return MergeResult(("\n".join(lines) + "\n").encode("utf-8"), False, "merged_gitattributes")


def _text_merge(base: bytes, current: bytes, desired: bytes) -> MergeResult:
    if not all(_is_text(item) for item in (base, current, desired)):
        return MergeResult(None, True, "binary_text_input")
    base_lines = base.decode("utf-8").splitlines(keepends=True)
    current_lines = current.decode("utf-8").splitlines(keepends=True)
    desired_lines = desired.decode("utf-8").splitlines(keepends=True)
    current_edits = _line_edits(base_lines, current_lines)
    desired_edits = _line_edits(base_lines, desired_lines)
    for left in current_edits:
        for right in desired_edits:
            if _edits_conflict(left, right):
                return MergeResult(None, True, "overlapping_text_change")
    edits: list[tuple[int, int, list[str]]] = []
    for edit in current_edits + desired_edits:
        if edit not in edits:
            edits.append(edit)
    merged = list(base_lines)
    for start, end, replacement in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        merged[start:end] = replacement
    return MergeResult("".join(merged).encode("utf-8"), False, "merged_text")


def _binary_merge(base: bytes, current: bytes, desired: bytes) -> MergeResult:
    return MergeResult(None, True, "binary_merge_forbidden")


def _merge_value(base: Any, current: Any, desired: Any) -> tuple[Any, bool]:
    if current == desired:
        return current, False
    if current == base:
        return desired, False
    if desired == base:
        return current, False
    if all(isinstance(item, dict) for item in (base, current, desired)):
        result: dict[str, Any] = {}
        for key in sorted(set(base) | set(current) | set(desired)):
            merged, conflict = _merge_value(
                base.get(key, _MISSING),
                current.get(key, _MISSING),
                desired.get(key, _MISSING),
            )
            if conflict:
                return None, True
            if merged is not _MISSING:
                result[key] = merged
        return result, False
    return None, True


def _line_edits(base: list[str], variant: list[str]) -> list[tuple[int, int, list[str]]]:
    edits: list[tuple[int, int, list[str]]] = []
    matcher = difflib.SequenceMatcher(a=base, b=variant, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            edits.append((i1, i2, variant[j1:j2]))
    return edits


def _edits_conflict(
    left: tuple[int, int, list[str]],
    right: tuple[int, int, list[str]],
) -> bool:
    if left == right:
        return False
    l1, l2, _ = left
    r1, r2, _ = right
    left_insert = l1 == l2
    right_insert = r1 == r2
    if left_insert and right_insert:
        return l1 == r1
    if left_insert:
        return r1 <= l1 <= r2
    if right_insert:
        return l1 <= r1 <= l2
    return max(l1, r1) < min(l2, r2)


def _ini_load(payload: bytes) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(payload.decode("utf-8"))
    return {
        section: {key: value for key, value in parser.items(section, raw=True)}
        for section in parser.sections()
    }


def _ini_dump(value: dict[str, dict[str, str]]) -> bytes:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    for section in sorted(value):
        parser[section] = {key: value[section][key] for key in sorted(value[section])}
    output = io.StringIO()
    parser.write(output, space_around_delimiters=True)
    return output.getvalue().encode("utf-8")


def _attributes_load(payload: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in payload.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        pattern = parts[0]
        attributes = parts[1] if len(parts) == 2 else ""
        if pattern in result:
            raise MergeDriverError(f"duplicate pattern {pattern!r}")
        result[pattern] = attributes
    return result


def _toml_dumps(value: dict[str, Any]) -> str:
    if not isinstance(value, dict):
        raise MergeDriverError("TOML root must be a table")
    lines: list[str] = []
    _toml_table(lines, (), value, emit_header=False)
    return "\n".join(lines).rstrip() + "\n"


def _toml_table(
    lines: list[str],
    path: tuple[str, ...],
    value: dict[str, Any],
    *,
    emit_header: bool,
) -> None:
    scalars = {key: item for key, item in value.items() if not isinstance(item, dict)}
    tables = {key: item for key, item in value.items() if isinstance(item, dict)}
    if emit_header:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("[" + ".".join(_toml_key(key) for key in path) + "]")
    for key in sorted(scalars):
        lines.append(f"{_toml_key(key)} = {_toml_value(scalars[key])}")
    for key in sorted(tables):
        _toml_table(lines, (*path, key), tables[key], emit_header=True)


def _toml_key(value: str) -> str:
    if _TOML_BARE_KEY.fullmatch(value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MergeDriverError("non-finite TOML float")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, list):
        if any(isinstance(item, dict) for item in value):
            raise MergeDriverError("arrays of tables are not supported by conservative merge")
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise MergeDriverError(f"unsupported TOML value {type(value).__name__}")


def _is_text(payload: bytes) -> bool:
    if b"\x00" in payload:
        return False
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
